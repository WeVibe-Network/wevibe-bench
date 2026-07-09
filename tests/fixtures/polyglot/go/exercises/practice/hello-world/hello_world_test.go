package hello

import "testing"

func TestHelloWorld(t *testing.T) {
	if HelloWorld() != "Hello, World!" {
		t.Fatalf("unexpected greeting")
	}
}
