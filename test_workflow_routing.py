"""Routing regression: Linux CI runs locally, macOS/Windows/release stay hosted."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SWITCH = "${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}"
LINUX_SUITE = (
    "${{ matrix.os == 'ubuntu-latest' && vars.CI_RUNNER "
    "&& fromJSON(vars.CI_RUNNER) || matrix.os }}"
)


class WorkflowRoutingTests(unittest.TestCase):
    def test_linux_suite_uses_the_runner_and_macos_stays_hosted(self):
        text = (ROOT / ".github/workflows/verify.yml").read_text()
        self.assertIn(f"runs-on: {LINUX_SUITE}", text)
        self.assertIn(f"runs-on: {SWITCH}", text)  # wheel
        self.assertIn("runs-on: windows-latest", text)

    def test_release_stays_hosted(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertNotIn("CI_RUNNER", text)

    def test_routing_check_is_wired_into_ci(self):
        text = (ROOT / ".github/workflows/verify.yml").read_text()
        self.assertIn("python -m unittest test_workflow_routing", text)


if __name__ == "__main__":
    unittest.main()
