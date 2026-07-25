"""Docker worker isolation primitives for backgammon benchmark cells."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable

from wevibe_bench.config import RunConfig


WORKER_IMAGE = "wevibe-bench-worker:v1"
WORKER_NETWORK = "wevibe-bench-net"


def _default_primary_recall_mode() -> str:
    return str(RunConfig().primary_recall_mode)


def _default_primary_recall_relevance_floor() -> float:
    return float(RunConfig().primary_recall_relevance_floor)


def _default_primary_recall_max_injected() -> int:
    return int(RunConfig().primary_recall_max_injected)


def _default_served_memories_host_path() -> str:
    return str(RunConfig().served_memories_host_path)


def _default_served_memories_container_path() -> str:
    return str(RunConfig().served_memories_container_path)


def docker_available() -> tuple[bool, str]:
    """Return Docker daemon availability and detail, never raising."""
    cmd = ["docker", "version", "--format", "{{.Server.Version}}"]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False, "docker CLI not found in PATH"
    except Exception as exc:  # noqa: BLE001 - contract requires never raising.
        return False, f"docker availability probe failed: {exc}"

    if completed.returncode != 0:
        detail = _result_detail(completed)
        return False, detail or "docker daemon unavailable"

    version = completed.stdout.strip()
    if not version:
        return False, "docker daemon reported empty version"
    return True, version


def image_exists(tag: str = WORKER_IMAGE) -> bool:
    """Return true when the requested worker image tag exists locally."""
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", tag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:  # noqa: BLE001 - boolean probe API must not raise.
        return False
    return completed.returncode == 0


def ensure_network(name: str = WORKER_NETWORK) -> None:
    """Ensure the benchmark bridge network exists (idempotent)."""
    inspect_cmd = ["docker", "network", "inspect", name]
    create_cmd = ["docker", "network", "create", name]

    try:
        inspected = subprocess.run(
            inspect_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker CLI not found in PATH") from exc
    except Exception as exc:  # noqa: BLE001 - include full failure detail.
        raise RuntimeError(f"docker network inspect failed for {name}: {exc}") from exc

    if inspected.returncode == 0:
        return

    created = subprocess.run(
        create_cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode == 0:
        return

    detail = _result_detail(created).lower()
    if "already exists" in detail:
        return
    raise RuntimeError(f"docker network create failed for {name}: {_result_detail(created)}")


def build_worker_image(
    *,
    context_dir: Path | None = None,
    tag: str = WORKER_IMAGE,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Build the disposable worker image, streaming docker build output."""
    context = _default_context_dir() if context_dir is None else Path(context_dir).expanduser().resolve()
    if not context.is_dir():
        raise RuntimeError(f"docker worker context does not exist: {context}")

    cmd = ["docker", "build", "-t", tag, str(context)]
    _emit(progress, f"PROGRESS docker-build start tag={tag} context={context}")

    tail: deque[str] = deque(maxlen=120)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker CLI not found in PATH") from exc
    except Exception as exc:  # noqa: BLE001 - include full failure detail.
        raise RuntimeError(f"docker build launch failed for {tag}: {exc}") from exc

    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        if not line:
            continue
        tail.append(line)
        _emit(progress, f"PROGRESS docker-build line={line}")
    proc.stdout.close()

    rc = proc.wait()
    if rc != 0:
        detail = " | ".join(tail) if tail else "no docker build output captured"
        raise RuntimeError(f"docker build failed tag={tag} context={context} rc={rc} detail={detail}")

    _emit(progress, f"PROGRESS docker-build complete tag={tag} context={context}")


@dataclass
class DockerCellConfig:
    worktree: Path
    memory_mode: str
    container_name: str
    image: str = WORKER_IMAGE
    network: str = WORKER_NETWORK
    recall_url: str = "http://host.docker.internal:4550"
    hub_url: str = "http://host.docker.internal:4440"
    # Primary scored path defaults to RunConfig.primary_recall_mode (prod).
    # Diagnostic/non-primary paths can still override this field (for example, test mode).
    recall_mode: str = field(default_factory=_default_primary_recall_mode)
    primary_recall_relevance_floor: float = field(default_factory=_default_primary_recall_relevance_floor)
    primary_recall_max_injected: int = field(default_factory=_default_primary_recall_max_injected)
    served_memories_host_path: str = field(default_factory=_default_served_memories_host_path)
    served_memories_container_path: str = field(default_factory=_default_served_memories_container_path)
    plugin_state_host_path: str = "~/.wevibe/state"
    plugin_state_container_path: str = "/work/.wevibe/state"
    plugin_config_host_path: str = "~/.wevibe/plugin-config.json"
    proxy_base_url: str | None = None
    proxy_token: str | None = None
    home_dir: str = "/home/worker"
    output_token_max: int | None = None
    worker_logs_dir: Path | None = None


class DockerCell:
    """Context-managed Docker cell lifecycle for isolated benchmark workers."""

    def __init__(
        self,
        config: DockerCellConfig,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.container_name = config.container_name
        self.container_id: str | None = None
        self._progress_cb = progress

    def __enter__(self) -> DockerCell:
        mode = self.config.memory_mode.strip().lower()
        if mode not in {"off", "on"}:
            raise ValueError("DockerCellConfig.memory_mode must be 'off' or 'on'")

        worktree = Path(self.config.worktree).expanduser().resolve()
        worktree.mkdir(parents=True, exist_ok=True)

        proxy_token = (self.config.proxy_token or "").strip()
        if not proxy_token:
            raise ValueError(
                "proxy token required; direct OpenRouter key forwarding is removed "
                "(R-13 one path, no fallback)"
            )

        key_present = True
        key_len = len(proxy_token)

        self._progress(f"PROGRESS docker-network ensure name={self.config.network}")
        ensure_network(self.config.network)
        self._progress(f"PROGRESS docker-network ready name={self.config.network}")

        uid = _host_uid()
        gid = _host_gid()
        mount = f"{worktree}:/work"
        run_cmd = _build_run_argv(
            config=self.config,
            worktree=worktree,
            uid=uid,
            gid=gid,
            memory_mode=mode,
        )

        if mode == "on":
            self._progress(
                "PROGRESS recall-primary-config "
                f"mode={str(self.config.recall_mode).strip().lower()} "
                f"served_store_host={_resolve_host_path(self.config.served_memories_host_path)} "
                f"served_store_container={self.config.served_memories_container_path} "
                f"plugin_state_host={_resolve_host_path(self.config.plugin_state_host_path)} "
                f"plugin_state_container={self.config.plugin_state_container_path} "
                f"recall_relevance_floor={float(self.config.primary_recall_relevance_floor):.6g} "
                f"recall_max_injected={int(self.config.primary_recall_max_injected)}"
            )

        run_env = os.environ.copy()
        run_env["OPENROUTER_API_KEY"] = proxy_token

        self._progress(
            "PROGRESS docker-run start "
            f"name={self.container_name} image={self.config.image} mount={mount} "
            f"memory_mode={mode} key_present={str(key_present).lower()} key_len={key_len} uid_gid={uid}:{gid}"
        )

        try:
            started = subprocess.run(
                run_cmd,
                env=run_env,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("docker CLI not found in PATH") from exc
        except Exception as exc:  # noqa: BLE001 - include full failure detail.
            raise RuntimeError(f"docker run failed to launch container {self.container_name}: {exc}") from exc

        if started.returncode != 0:
            detail = _result_detail(started)
            self._progress(
                f"PROGRESS docker-run fail name={self.container_name} rc={started.returncode} detail={detail}"
            )
            raise RuntimeError(
                f"docker run failed name={self.container_name} image={self.config.image} "
                f"rc={started.returncode} detail={detail}"
            )

        container_id = started.stdout.strip()
        if not container_id:
            raise RuntimeError(
                f"docker run returned empty container id for name={self.container_name} image={self.config.image}"
            )

        self.container_id = container_id
        self._progress(
            "PROGRESS docker-run ready "
            f"name={self.container_name} container_id={container_id[:12]} image={self.config.image} memory_mode={mode}"
        )
        return self

    def exec_argv(self, inner_argv: list[str]) -> list[str]:
        return ["docker", "exec", "-i", "-w", "/work", self.container_name, *inner_argv]

    def kill_worker_processes(self) -> None:
        if not self.container_name:
            return

        kill_cmd = self.exec_argv(["sh", "-lc", "pkill -9 -f '[o]pencode' || true"])
        self._progress(f"PROGRESS worker-process-kill start name={self.container_name}")

        try:
            killed = subprocess.run(
                kill_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            self._progress(
                f"PROGRESS worker-process-kill fail name={self.container_name} reason=docker_cli_missing"
            )
            raise RuntimeError("docker CLI not found in PATH") from exc
        except Exception as exc:  # noqa: BLE001 - kill hook reports full detail upstream.
            self._progress(
                f"PROGRESS worker-process-kill fail name={self.container_name} reason=exception detail={exc}"
            )
            raise RuntimeError(
                f"docker exec process-kill failed for name={self.container_name}: {exc}"
            ) from exc

        detail = _result_detail(killed)
        if killed.returncode != 0:
            self._progress(
                f"PROGRESS worker-process-kill fail name={self.container_name} rc={killed.returncode} detail={detail}"
            )
            raise RuntimeError(
                f"docker exec process-kill failed name={self.container_name} "
                f"rc={killed.returncode} detail={detail}"
            )

        self._progress(
            f"PROGRESS worker-process-kill done name={self.container_name} detail={detail or 'pkill-ok'}"
        )

    def teardown(self) -> None:
        if not self.container_name:
            return

        self._capture_worker_logs_pre_teardown()

        self._progress(f"PROGRESS docker-rm start name={self.container_name}")

        try:
            removed = subprocess.run(
                ["docker", "rm", "-f", self.container_name],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            self._progress(f"PROGRESS docker-rm fail name={self.container_name} reason=docker_cli_missing")
            self.container_id = None
            return
        except Exception as exc:  # noqa: BLE001 - teardown must not raise.
            self._progress(
                f"PROGRESS docker-rm fail name={self.container_name} reason=exception detail={exc}"
            )
            self.container_id = None
            return

        detail = _result_detail(removed)
        if removed.returncode == 0:
            self._progress(
                f"PROGRESS docker-rm done name={self.container_name} detail={detail or 'removed'}"
            )
        elif "No such container" in detail:
            self._progress(f"PROGRESS docker-rm done name={self.container_name} detail=already-absent")
        else:
            self._progress(
                f"PROGRESS docker-rm fail name={self.container_name} rc={removed.returncode} detail={detail}"
            )

        self.container_id = None

    def _capture_worker_logs_pre_teardown(self) -> None:
        if self.config.worker_logs_dir is None:
            self._progress(
                "INFO op=worker_logs.capture step=skip path=none bytes=0 "
                "status=skipped reason=worker_logs_dir_none"
            )
            return

        destination = Path(self.config.worker_logs_dir).expanduser().resolve()
        inspect_path = destination / "container-inspect.json"
        docker_logs_path = destination / "container-docker.log"
        opencode_path = destination / "opencode"

        try:
            destination.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 - teardown must never raise.
            reason = self._capture_reason(f"destination_mkdir_failed detail={exc}")
            self._log_worker_capture(
                step="inspect",
                path=inspect_path,
                bytes_count=0,
                status="failed",
                reason=reason,
            )
            self._log_worker_capture(
                step="docker_logs",
                path=docker_logs_path,
                bytes_count=0,
                status="failed",
                reason=reason,
            )
            self._log_worker_capture(
                step="cp",
                path=opencode_path,
                bytes_count=0,
                status="failed",
                reason=reason,
            )
            return

        try:
            inspected = subprocess.run(
                ["docker", "inspect", self.container_name],
                capture_output=True,
                text=True,
                check=False,
            )
            if inspected.returncode != 0:
                self._log_worker_capture(
                    step="inspect",
                    path=inspect_path,
                    bytes_count=0,
                    status="failed",
                    reason=f"rc={inspected.returncode} detail={_result_detail(inspected)}",
                )
            else:
                inspect_path.write_text(inspected.stdout or "", encoding="utf-8")
                self._log_worker_capture(
                    step="inspect",
                    path=inspect_path,
                    bytes_count=self._path_bytes(inspect_path),
                    status="ok",
                    reason="none",
                )
        except Exception as exc:  # noqa: BLE001 - teardown must never raise.
            self._log_worker_capture(
                step="inspect",
                path=inspect_path,
                bytes_count=0,
                status="failed",
                reason=f"exception detail={exc}",
            )

        try:
            docker_logs = subprocess.run(
                ["docker", "logs", "--timestamps", self.container_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            docker_logs_path.write_text(docker_logs.stdout or "", encoding="utf-8")
            bytes_count = self._path_bytes(docker_logs_path)
            if docker_logs.returncode != 0:
                self._log_worker_capture(
                    step="docker_logs",
                    path=docker_logs_path,
                    bytes_count=bytes_count,
                    status="failed",
                    reason=f"rc={docker_logs.returncode} detail={_result_detail(docker_logs)}",
                )
            else:
                self._log_worker_capture(
                    step="docker_logs",
                    path=docker_logs_path,
                    bytes_count=bytes_count,
                    status="ok",
                    reason="none",
                )
        except Exception as exc:  # noqa: BLE001 - teardown must never raise.
            self._log_worker_capture(
                step="docker_logs",
                path=docker_logs_path,
                bytes_count=0,
                status="failed",
                reason=f"exception detail={exc}",
            )

        try:
            opencode_path.mkdir(parents=True, exist_ok=True)

            staging_host_path = Path(self.config.worktree).expanduser().resolve() / ".wevibe-worker-log-export"
            try:
                shutil.rmtree(staging_host_path)
            except FileNotFoundError:
                pass

            staged = subprocess.run(
                self.exec_argv(
                    [
                        "sh",
                        "-lc",
                        (
                            "mkdir -p /work/.wevibe-worker-log-export/opencode "
                            f"&& cp -a {self.config.home_dir}/.local/share/opencode/. "
                            "/work/.wevibe-worker-log-export/opencode/"
                        ),
                    ]
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            if staged.returncode != 0:
                self._log_worker_capture(
                    step="cp",
                    path=opencode_path,
                    bytes_count=self._path_bytes(opencode_path),
                    status="failed",
                    reason=f"stage_rc={staged.returncode} detail={_result_detail(staged)}",
                )
            else:
                copied = subprocess.run(
                    [
                        "docker",
                        "cp",
                        f"{self.container_name}:/work/.wevibe-worker-log-export/opencode/.",
                        str(opencode_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if copied.returncode != 0:
                    self._log_worker_capture(
                        step="cp",
                        path=opencode_path,
                        bytes_count=self._path_bytes(opencode_path),
                        status="failed",
                        reason=f"rc={copied.returncode} detail={_result_detail(copied)}",
                    )
                else:
                    self._log_worker_capture(
                        step="cp",
                        path=opencode_path,
                        bytes_count=self._path_bytes(opencode_path),
                        status="ok",
                        reason="none",
                    )

            try:
                shutil.rmtree(staging_host_path)
            except FileNotFoundError:
                pass
        except Exception as exc:  # noqa: BLE001 - teardown must never raise.
            self._log_worker_capture(
                step="cp",
                path=opencode_path,
                bytes_count=0,
                status="failed",
                reason=f"exception detail={exc}",
            )

    def _log_worker_capture(
        self,
        *,
        step: str,
        path: Path,
        bytes_count: int,
        status: str,
        reason: str,
    ) -> None:
        self._progress(
            "PROGRESS op=worker_logs.capture "
            f"step={step} path={path} bytes={max(0, int(bytes_count))} "
            f"status={status} reason={self._capture_reason(reason)}"
        )

    @staticmethod
    def _capture_reason(reason: str) -> str:
        normalized = " ".join(str(reason).split())
        if not normalized:
            return "none"
        if len(normalized) > 512:
            return f"{normalized[:509]}..."
        return normalized

    @staticmethod
    def _path_bytes(path: Path) -> int:
        candidate = Path(path)
        if not candidate.exists():
            return 0
        if candidate.is_file():
            return max(0, int(candidate.stat().st_size))
        total = 0
        for child in candidate.rglob("*"):
            if child.is_file():
                total += max(0, int(child.stat().st_size))
        return total

    def force_kill(self) -> None:
        self.teardown()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.teardown()

    def _progress(self, message: str) -> None:
        _emit(self._progress_cb, message)


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is None:
        return
    progress(message)


def _default_context_dir() -> Path:
    return (Path(__file__).resolve().parents[2] / "docker" / "worker").resolve()


def _build_run_argv(
    *,
    config: DockerCellConfig,
    worktree: Path,
    uid: int,
    gid: int,
    memory_mode: str,
) -> list[str]:
    """Build the docker run argv for a worker cell without touching process env."""
    mount = f"{Path(worktree).expanduser().resolve()}:/work"
    run_cmd: list[str] = [
        "docker",
        "run",
        "-d",
        "--name",
        config.container_name,
        "--network",
        config.network,
        "--add-host",
        "host.docker.internal:host-gateway",
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        # tmpfs mounts default to root:root 0755; the worker runs as the host
        # (non-root) uid via --user, so HOME/tmp must be world-writable (1777,
        # standard /tmp semantics) or opencode's ~/.local/share mkdir fails with
        # EACCES. Isolation is preserved by --read-only + cap-drop ALL +
        # no-new-privileges + external_directory deny, not by tmpfs ownership.
        "--tmpfs",
        "/tmp:mode=1777",
        "--tmpfs",
        f"{config.home_dir}:mode=1777",
        "-e",
        f"HOME={config.home_dir}",
        # The image pins XDG_CONFIG_HOME/OPENCODE_CONFIG_DIR under /etc/xdg on the
        # --read-only root, but opencode must WRITE state (e.g. its config-dir
        # .gitignore) or it aborts with "FileSystem.writeFile". Redirect the XDG +
        # opencode state dirs into the writable HOME tmpfs; the baked config file
        # is still loaded via OPENCODE_CONFIG (read-only reads are fine).
        "-e",
        f"XDG_CONFIG_HOME={config.home_dir}/.config",
        "-e",
        f"XDG_DATA_HOME={config.home_dir}/.local/share",
        "-e",
        f"XDG_CACHE_HOME={config.home_dir}/.cache",
        "-e",
        f"OPENCODE_CONFIG_DIR={config.home_dir}/.config/opencode",
        "-e",
        "OPENCODE_CONFIG=/etc/xdg/opencode/opencode.json",
        "-e",
        "OPENROUTER_API_KEY",
        "-v",
        mount,
    ]

    if config.output_token_max is not None:
        # Enforced output-cap lever for opencode workers: this EXPERIMENTAL env flag
        # maps to AI-SDK `maxOutputTokens`, which then serializes to request-body
        # `max_tokens`. It only constrains completions when the configured value is
        # <= the selected model's own output-token limit. For the worker image's
        # exact pinned opencode version, honoring is still a live assertion pending
        # request-body capture.
        run_cmd.extend(
            [
                "-e",
                f"OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX={config.output_token_max}",
            ]
        )

    if memory_mode == "on":
        host_token = _resolve_host_path("~/.wevibe/mcp-session-token")
        if not host_token.is_file():
            raise FileNotFoundError(
                "memory_mode='on' requires host token ~/.wevibe/mcp-session-token; "
                "start the wevibe-mcp clone or run bench preflight to mint it"
            )

        recall_mode = str(config.recall_mode).strip().lower()
        if not recall_mode:
            raise ValueError("DockerCellConfig.recall_mode must be non-empty when memory_mode='on'")

        host_served_memories = _resolve_host_path(config.served_memories_host_path)
        _ensure_served_memories_store(host_served_memories)

        host_plugin_state = _resolve_host_path(config.plugin_state_host_path)
        _ensure_plugin_state_dir(host_plugin_state)

        host_plugin_config = _resolve_host_path(config.plugin_config_host_path)
        _merge_plugin_config(
            host_plugin_config,
            recall_relevance_floor=config.primary_recall_relevance_floor,
            recall_max_injected=config.primary_recall_max_injected,
        )

        token_dest = f"{config.home_dir}/.wevibe/mcp-session-token"
        plugin_config_dest = f"{config.home_dir}/.wevibe/plugin-config.json"

        run_cmd.extend(
            [
                "-e",
                f"WEVIBE_MCP_HTTP_URL={config.recall_url}",
                "-e",
                f"WEVIBE_RECALL_MODE={recall_mode}",
                "-e",
                f"WEVIBE_HUB_URL={config.hub_url}",
                "-e",
                f"WEVIBE_SERVED_MEMORIES_PATH={config.served_memories_container_path}",
                # Vendored wevibe plugin hardcodes ~/.wevibe/mcp-session-token and
                # the clone API is bearer-gated; mount that token only, read-only.
                "-v",
                f"{host_token}:{token_dest}:ro",
                # The plugin reads ~/.wevibe/plugin-config.json from homedir(); mount
                # a host-authored config so primary governor values are explicit.
                "-v",
                f"{host_plugin_config}:{plugin_config_dest}:ro",
                # Shared served-store file bridges container writes back to the host.
                "-v",
                f"{host_served_memories}:{config.served_memories_container_path}:rw",
                # Shared plugin-state directory bridges queue/decisions/heartbeat.
                "-v",
                f"{host_plugin_state}:{config.plugin_state_container_path}:rw",
            ]
        )

    run_cmd.extend([config.image, "sleep", "infinity"])
    return run_cmd


def _result_detail(completed: subprocess.CompletedProcess[str]) -> str:
    stderr = completed.stderr.strip() if completed.stderr else ""
    stdout = completed.stdout.strip() if completed.stdout else ""
    detail = stderr or stdout
    return detail or f"exit={completed.returncode}"


def _resolve_host_path(raw_path: str) -> Path:
    return Path(str(raw_path)).expanduser().resolve()


def _ensure_served_memories_store(path: Path) -> None:
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)

    if resolved.exists() and not resolved.is_file():
        raise RuntimeError(f"served-memories path must be a file: {resolved}")

    if not resolved.exists():
        resolved.write_text('{"version":1,"memories":{}}', encoding="utf-8")

    resolved.chmod(0o600)


def _ensure_plugin_state_dir(path: Path) -> None:
    resolved = Path(path).expanduser().resolve()

    if resolved.exists() and not resolved.is_dir():
        raise RuntimeError(f"plugin-state path must be a directory: {resolved}")

    resolved.mkdir(parents=True, exist_ok=True)
    resolved.chmod(0o700)


def _merge_plugin_config(
    path: Path,
    *,
    recall_relevance_floor: float,
    recall_max_injected: int,
) -> None:
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, object] = {}
    if resolved.exists():
        if not resolved.is_file():
            raise RuntimeError(f"plugin-config path must be a file: {resolved}")
        raw = resolved.read_text(encoding="utf-8").strip()
        if raw:
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"plugin-config at {resolved} is invalid JSON: {exc}") from exc
            if not isinstance(decoded, dict):
                raise RuntimeError(f"plugin-config at {resolved} must decode to a JSON object")
            payload = dict(decoded)

    payload["recall_relevance_floor"] = float(recall_relevance_floor)
    payload["recall_max_injected"] = int(recall_max_injected)

    resolved.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    resolved.chmod(0o600)


def _host_uid() -> int:
    getter = getattr(os, "getuid", None)
    if callable(getter):
        return int(getter())
    return 0


def _host_gid() -> int:
    getter = getattr(os, "getgid", None)
    if callable(getter):
        return int(getter())
    return 0
