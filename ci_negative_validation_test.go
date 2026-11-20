package main

import "testing"

func TestCIValidationFailsIntentionally(t *testing.T) {
	t.Fatal("Intentional CI validation failure. This branch must not be merged.")
}
