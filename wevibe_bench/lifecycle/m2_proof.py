"""Lifecycle milestone-2 proof driver (recall-delivery verification only)."""

from __future__ import annotations

from typing import Any, Callable

from .lconfig import LifecycleConfig
from .logging_util import fp, new_trace_id
from .mcp_rest import McpRest


McpRestFactory = Callable[[str], Any]


class M2Proof:
    _MEMORY_FRAGMENT_LIMIT = 64

    def __init__(
        self,
        cfg: LifecycleConfig,
        orchestrator: Any,
        logger: Any,
        *,
        mcp_rest_factory: McpRestFactory | None = None,
    ) -> None:
        self._cfg = cfg
        self._orchestrator = orchestrator
        self._logger = logger

        self._mcp_rest_factory = mcp_rest_factory or (
            lambda base_url: McpRest(base_url, self._cfg, self._logger)
        )

    @property
    def orchestrator(self) -> Any:
        return self._orchestrator

    @staticmethod
    def _sanitize(value: Any) -> str:
        return " ".join(str(value).split())

    def _log(
        self,
        level: str,
        op: str,
        trace: str,
        status: str,
        dur_ms: int,
        **fields: Any,
    ) -> None:
        details = " ".join(
            f"{key}={self._sanitize(value)}"
            for key, value in fields.items()
            if value is not None
        )
        msg = f"op={op} trace={trace}"
        if details:
            msg = f"{msg} {details}"
        msg = f"{msg} status={status} dur_ms={dur_ms}"
        log_fn = getattr(self._logger, level, self._logger.info)
        log_fn(msg)

    def _leader_rest(self) -> Any:
        return self._mcp_rest_factory(self._cfg.leader_mcp_url)

    @classmethod
    def memory_fragment(cls, text: str) -> str:
        compact = " ".join(str(text).split())
        return compact[: cls._MEMORY_FRAGMENT_LIMIT]

    def prove_delivery(self, org_id: str, expected_text_fragment: str | list[Any]) -> dict[str, Any]:
        raw_targets: list[Any]
        if isinstance(expected_text_fragment, list):
            raw_targets = expected_text_fragment
        elif isinstance(expected_text_fragment, str):
            raw_targets = [expected_text_fragment] if expected_text_fragment.strip() else []
        else:
            raise RuntimeError(
                "prove_delivery expected a fragment string or list of strings/{fragment,cid} targets"
            )

        targets: list[dict[str, str | None]] = []
        for raw_target in raw_targets:
            fragment_source: Any = None
            cid_source: Any = None

            if isinstance(raw_target, str):
                fragment_source = raw_target
            elif isinstance(raw_target, dict):
                fragment_source = raw_target.get("fragment")
                cid_source = raw_target.get("cid")
            elif isinstance(raw_target, tuple) and len(raw_target) == 2:
                fragment_source, cid_source = raw_target
            elif isinstance(raw_target, list) and len(raw_target) == 2:
                fragment_source, cid_source = raw_target[0], raw_target[1]
            else:
                raise RuntimeError(
                    "prove_delivery list items must be str, {fragment,cid} dict, or (fragment,cid) pair"
                )

            fragment = self.memory_fragment(str(fragment_source or ""))
            if not fragment:
                continue

            cid: str | None = None
            if cid_source is not None:
                cid_value = str(cid_source).strip()
                cid = cid_value or None

            targets.append({"fragment": fragment, "cid": cid})

        if not targets:
            return {
                "delivery": "NO",
                "n_memories": 0,
                "matched": False,
                "any_matched": False,
                "per_memory": [],
            }

        leader_rest = self._leader_rest()
        delivery_trace = new_trace_id()
        per_memory: list[dict[str, Any]] = []
        all_delivered = True
        all_matched = True
        any_matched = False

        for target in targets:
            fragment = str(target["fragment"])
            cid = target["cid"]

            payload = leader_rest.recall(
                query=fragment,
                org_id=org_id,
            )
            memories = payload.get("memories") if isinstance(payload, dict) else None
            memory_items = memories if isinstance(memories, list) else []
            returned_cids = {
                str(item.get("cid")).strip()
                for item in memory_items
                if isinstance(item, dict) and isinstance(item.get("cid"), str) and str(item.get("cid")).strip()
            }

            fragment_lc = fragment.lower()
            matched = False
            for item in memory_items:
                text = item.get("text") if isinstance(item, dict) else None
                if isinstance(text, str) and fragment_lc and fragment_lc in text.lower():
                    matched = True
                    break

            delivered = matched
            delivery_mode = "matched" if matched else "unmatched"
            suppression_entry: dict[str, Any] | None = None

            if not matched and isinstance(cid, str) and cid:
                suppression = payload.get("suppression") if isinstance(payload, dict) else None
                if isinstance(suppression, dict):
                    dropped_raw = suppression.get("dropped_twin_cid")
                    winner_raw = suppression.get("winner_cid")
                    dropped_cid = dropped_raw.strip() if isinstance(dropped_raw, str) else ""
                    winner_cid = winner_raw.strip() if isinstance(winner_raw, str) else ""
                    score_gap_raw = suppression.get("score_gap")
                    score_gap: float | None = None
                    if isinstance(score_gap_raw, (int, float)) and not isinstance(score_gap_raw, bool):
                        score_gap = float(score_gap_raw)

                    if dropped_cid and dropped_cid == cid:
                        suppression_entry = {
                            "winner_cid": fp(winner_cid) if winner_cid else None,
                            "dropped_twin_cid": fp(dropped_cid),
                            "score_gap": score_gap,
                        }
                        if winner_cid and winner_cid in returned_cids:
                            delivered = True
                            delivery_mode = "twin_of_returned"
                            self._log(
                                "info",
                                "lifecycle.m2.delivery_twin",
                                delivery_trace,
                                "ok",
                                0,
                                fragment_fp=fp(fragment),
                                cid_fp=fp(cid),
                                winner_cid_fp=fp(winner_cid),
                                dropped_twin_cid_fp=fp(dropped_cid),
                                score_gap=score_gap,
                            )
                        else:
                            delivered = False
                            delivery_mode = "suppressed_winner_absent"

            memory_entry: dict[str, Any] = {
                "fragment_fp": fp(fragment),
                "cid": fp(cid) if isinstance(cid, str) and cid else None,
                "matched": matched,
                "delivered": delivered,
                "delivery_mode": delivery_mode,
            }
            if suppression_entry is not None:
                memory_entry["suppression"] = suppression_entry
            per_memory.append(memory_entry)

            all_delivered = all_delivered and delivered
            all_matched = all_matched and matched
            any_matched = any_matched or matched

        return {
            "delivery": "YES" if all_delivered else "NO",
            "n_memories": len(per_memory),
            "matched": all_matched,
            "any_matched": any_matched,
            "per_memory": per_memory,
        }
