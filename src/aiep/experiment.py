"""The pre-registered experiment definition: prompts, Claude CLI pin and arguments, shared by Config A and Config B.

Config A (basic: investigate, fix, stop) and Config B (A plus run tests, analyse failures, repeat) must differ ONLY in the
prescribed workflow. Everything else is built here from inputs that have no configuration parameter, so it cannot differ:

* the task text is one shared template with exactly one varying line (the workflow), so ``build_task_text`` for A and B
  is identical once that line is blanked (tested);
* ``claude_args`` takes no configuration label: both configurations run the same flags, tools and model;
* model, limits and image are chosen by the orchestrator once per run and apply to both labels alike.

The prompt texts live in ``config/workflow_prompts.json`` together with their SHA-256s. ``load_prompts`` refuses a file whose
text no longer matches its recorded hash, so an edit cannot go unnoticed, and every run records the hashes (and the hash of
the final prompt it actually sent).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .task_input import TaskInput

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_LABELS = ("A", "B")
ISSUE_PLACEHOLDER, WORKFLOW_PLACEHOLDER = "<<ISSUE>>", "<<WORKFLOW>>"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PromptError(RuntimeError):
    """The prompt file is unreadable, malformed, or does not match its recorded hashes."""


class ClaudeCliUnpinned(RuntimeError):
    """``config/claude_cli.json`` has no verified pin for the binary; nothing may be injected from it."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Prompts:
    template: str
    workflows: dict[str, str]
    hashes: dict[str, str]  # "template", "A", "B"

    def for_label(self, label: str) -> str:
        if label not in CONFIG_LABELS:
            raise PromptError(f"config label must be one of {list(CONFIG_LABELS)}")
        return self.workflows[label]


def load_prompts(path: Path | None = None) -> Prompts:
    path = Path(path) if path else REPO_ROOT / "config" / "workflow_prompts.json"
    try:
        doc: Any = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise PromptError(f"cannot read the prompt file: {type(e).__name__}") from e
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise PromptError("unsupported prompt file schema")
    template, workflows, recorded = doc.get("template"), doc.get("workflows"), doc.get("sha256")
    if not (isinstance(template, str) and isinstance(workflows, dict) and isinstance(recorded, dict)):
        raise PromptError("the prompt file is missing its template, workflows or hashes")
    if set(workflows) != set(CONFIG_LABELS) or set(recorded) != {"template", *CONFIG_LABELS}:
        raise PromptError("the prompt file must define exactly the workflows A and B and their hashes")
    if template.count(ISSUE_PLACEHOLDER) != 1 or template.count(WORKFLOW_PLACEHOLDER) != 1:
        raise PromptError("the template must contain each placeholder exactly once")
    texts = {"template": template, **workflows}
    for name, text in texts.items():
        if not isinstance(text, str) or not text.strip():
            raise PromptError(f"prompt {name!r} is empty or not text")
        if not (isinstance(recorded[name], str) and _SHA256.fullmatch(recorded[name])) or sha256_text(text) != recorded[name]:
            raise PromptError(f"prompt {name!r} does not match its recorded SHA-256; edits must update the hash deliberately")
    if ISSUE_PLACEHOLDER in "".join(workflows.values()) or WORKFLOW_PLACEHOLDER in "".join(workflows.values()):
        raise PromptError("a workflow must not contain a placeholder")
    return Prompts(template, dict(workflows), dict(recorded))


def build_task_text(label: str, task: TaskInput, prompts: Prompts | None = None) -> str:
    """The text delivered on the agent's stdin. Only the issue and the workflow line enter the shared template.

    Nothing else about the task (not the base commit, not any hidden field) is included. The workflow is substituted
    first and the issue last, so text inside the issue is never re-interpreted as a placeholder.
    """
    prompts = prompts or load_prompts()
    return (prompts.template.replace(WORKFLOW_PLACEHOLDER, prompts.for_label(label))
            .replace(ISSUE_PLACEHOLDER, task.problem_statement.strip("\n")))


def prompt_record(label: str, text: str, prompts: Prompts | None = None) -> dict[str, str]:
    """Hashes to store with a run: the registered template and workflow, and the exact text that was sent."""
    prompts = prompts or load_prompts()
    return {"label": label, "template_sha256": prompts.hashes["template"], "workflow_sha256": prompts.hashes[label],
            "prompt_sha256": sha256_text(text)}


# ------------------------------------------------------------------ Claude CLI
# Tools are the SAME for both configurations. WebSearch/WebFetch are never offered (the gateway refuses server-side
# tools and stops the run), and there are no MCP servers.
CLAUDE_TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
REGISTERED_PROMPT_SETS = ("config/workflow_prompts.json", "config/workflow_prompts_v2.json")  # the only prompt files a run may select (v1: retired from M3; v2: the M3 set)
MAX_BUDGET_USD = Decimal("100")  # a plain sanity ceiling; the real limit is the dedicated key and the gateway budget


# Claude Code's own ``--max-budget-usd`` is a CLIENT-SIDE estimate. It is set a little ABOVE the gateway's budget so the gateway, which meters real
# usage from the API's own usage fields, trips first and stays the authority; a run that ends with the CLI's ``error_max_budget_usd`` result
# was therefore stopped by the client's estimate, and the two causes are recorded separately (m2_run.budget_attribution).
CLIENT_BUDGET_MARGIN = Decimal("1.10")


def client_budget_usd(budget_usd: str) -> str:
    """The ``--max-budget-usd`` value for a gateway budget: ``budget * 1.10`` rounded UP to 4 decimals, as a plain decimal string."""
    gateway = Decimal(budget_usd)
    value = (gateway * CLIENT_BUDGET_MARGIN).quantize(Decimal("0.0001"), rounding=ROUND_UP)
    return format(value.normalize(), "f")


def claude_args(model: str, budget_usd: str) -> tuple[str, ...]:
    """Flags for the headless run. Takes no configuration label: A and B get exactly these.

    Verified only against ``claude --help`` of the locally installed macOS 2.1.328 (same release as the pinned Linux
    build; the Linux binary's own help is checked after Gate 1). ``--bare`` is deliberately absent: it accepts only an
    ANTHROPIC_API_KEY, which the agent policy forbids (the agent authenticates with ANTHROPIC_AUTH_TOKEN).
    ``--max-turns`` is not in the help text and is not used.
    """
    try:
        if not (isinstance(budget_usd, str) and re.fullmatch(r"\d{1,3}(\.\d{1,4})?", budget_usd)
                and 0 < Decimal(budget_usd) <= MAX_BUDGET_USD):
            raise ValueError
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ValueError(f"budget_usd must be a plain positive decimal string no larger than {MAX_BUDGET_USD}") from e
    if not re.fullmatch(r"[A-Za-z0-9._\-]{1,100}", model or ""):
        raise ValueError("model must be a plain model ID")
    return ("-p", "--output-format", "stream-json", "--verbose", "--safe-mode", "--permission-mode", "bypassPermissions",
            "--tools", CLAUDE_TOOLS, "--no-session-persistence", "--model", model, "--max-budget-usd", client_budget_usd(budget_usd))


@dataclass(frozen=True)
class ClaudeCliPin:
    version: str
    platform: str
    sha256: str
    size_bytes: int
    destination: str
    max_binary_bytes: int


def load_claude_cli(path: Path | None = None) -> ClaudeCliPin:
    """The verified Claude binary pin. Refuses anything but ``status: pinned`` with a real SHA-256 and size."""
    path = Path(path) if path else REPO_ROOT / "config" / "claude_cli.json"
    try:
        cfg: Any = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ClaudeCliUnpinned(f"cannot read the Claude CLI pin: {type(e).__name__}") from e
    if not isinstance(cfg, dict) or cfg.get("status") != "pinned":
        raise ClaudeCliUnpinned(f"Claude CLI status is {cfg.get('status') if isinstance(cfg, dict) else None!r}, not 'pinned'")
    sha, size, ceiling = cfg.get("sha256"), cfg.get("size_bytes"), cfg.get("max_binary_bytes")
    if not (isinstance(sha, str) and _SHA256.fullmatch(sha)):
        raise ClaudeCliUnpinned("the Claude CLI pin needs the SHA-256 of the actual binary")
    if not (type(size) is int and type(ceiling) is int and 0 < size <= ceiling):
        raise ClaudeCliUnpinned("the Claude CLI pin needs a positive size no larger than max_binary_bytes")
    if cfg.get("destination") != "/opt/aiep/claude" or cfg.get("platform") != "linux-x64":
        raise ClaudeCliUnpinned("the Claude CLI pin must be the linux-x64 build injected at /opt/aiep/claude")
    version = cfg.get("version")
    if not (isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version)):
        raise ClaudeCliUnpinned("the Claude CLI pin needs an exact version")
    return ClaudeCliPin(version, cfg["platform"], sha, size, cfg["destination"], ceiling)
