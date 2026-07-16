"""Interactive REPL - the user-facing terminal interface."""

import json
import sys
import os
import argparse
from typing import Any, Dict

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from .agent import Agent
from .llm import LLM, LiteLLM
from .config import Config
from .session import save_session, load_session, list_sessions
from . import __version__



console = Console()
BANNER = "=" * 70


def _parse_args():
    p = argparse.ArgumentParser(
        prog="coderover",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: $CORECODER_MODEL or gpt-4o)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    #以下是为了测试harness功能，调试用
    subparsers = p.add_subparsers(dest="command", help="Available commands")

    # --- test-harness (development/debug) ---
    th_parser = subparsers.add_parser("test-harness", help="Run Harness in a test target")
    th_parser.add_argument("-t", "--target", default="tests/test_target", help="Target directory to fix")
    th_parser.add_argument("--task", default="修复失败的测试", help="Task description")
    th_parser.add_argument("--max-retries", type=int, default=3, help="Maximum retry attempts")
    th_parser.add_argument("--aggressive", action="store_true", help="强制修复所有错误（包括低危警告，如 ruff、var-annotated）")

    # --- run (production entry) ---
    run_parser = subparsers.add_parser("run", help="Run CodeRover on a repository")
    run_parser.add_argument("--repo", required=False, help="Path to the repository to fix")
    run_parser.add_argument("--issue", help="GitHub Issue URL (e.g. https://github.com/owner/repo/issues/42)")
    run_parser.add_argument("--task", default="修复代码中的问题", help="Task description (ignored when --issue is used)")
    run_parser.add_argument("--max-retries", type=int, default=3, help="Maximum retry attempts")
    run_parser.add_argument("--aggressive", action="store_true", help="Fix all errors including low-severity warnings")
    run_parser.add_argument("--json-output", action="store_true", help="Output result as JSON (for CI integration)")
    run_parser.add_argument("--pr", action="store_true", help="Auto-create a GitHub PR on success")
    run_parser.add_argument("-m", "--model", help="Model name (overrides .env)")
    run_parser.add_argument("--base-url", help="API base URL (overrides .env)")
    run_parser.add_argument("--api-key", help="API key (overrides .env)")

    return p.parse_args()


def _run_harness_task(
    repo_path: str,
    task: str,
    max_retries: int,
    aggressive: bool,
    pr: bool,
    is_issue_task: bool = False,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Dict[str, Any]:
    """Shared logic: load config -> create LLM -> run Harness -> return result."""
    from pathlib import Path
    from coderover.core.harness import AdaptiveHarness
    from coderover.llm import LLM
    from coderover.tools import ALL_TOOLS
    from coderover.config import Config

    #print(f"[INFO] Task ({len(task)} chars): {task[:200]}...")

    # 1. Load config from env
    config = Config.from_env()

    # 2. CLI overrides
    if model:
        config.model = model
    if base_url:
        config.base_url = base_url
    if api_key:
        config.api_key = api_key

    # 3. Safety checks
    if not config.model:
        return {
            "status": "error",
            "reason": "No model specified. Set CORECODER_MODEL in .env or pass --model.",
        }

    target = Path(repo_path).resolve()
    if not target.exists():
        return {"status": "error", "reason": f"Target path does not exist: {target}"}

    print(f"  Starting CodeRover run  target={target}")
    print(f"  Model: {config.model}")
    print(BANNER)

    # 4. Create LLM + Harness
    llm = LLM(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    harness = AdaptiveHarness(llm, ALL_TOOLS, max_retries=max_retries, aggressive=aggressive)

    # 5. Execute
    result = harness.run(task=task, repo_path=str(target), is_issue_task=is_issue_task)

    # 6. Auto-create PR on success
    if pr and result.get("status") == "success":
        try:
            from coderover.tools.github_client import GitHubClient

            client = GitHubClient()
            pr_info = client.create_pr(
                repo="seon1212/Coderover",
                title=f"[CodeRover] Auto-fix: {task[:50]}",
                body=(
                    f"CodeRover auto-fix completed.\n\n"
                    f"Modified files: {result.get('modified_files', [])}\n"
                    f"Attempts: {result.get('attempts', 0)}"
                ),
                head="coderover-fix",
                base="main",
            )
            result["pr_url"] = pr_info.get("html_url")
            print(f"  PR created: {pr_info.get('html_url')}")
        except ValueError as e:
            print(f"  [WARN] PR creation skipped: {e}")
            result["pr_error"] = str(e)
        except ImportError as e:
            print(f"  [WARN] GitHubClient unavailable: {e}")

    return result


def _run_test_harness(args):
    """Execute Harness debug test (Uses Chinese output for clarity during debugging)."""
    result = _run_harness_task(
        repo_path=args.target,
        task=args.task,
        max_retries=args.max_retries,
        aggressive=args.aggressive,
        pr=False,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
    )
    if result.get("reason"):
        print(f"Harness execution failed: {result['reason']}")
        return

    print(BANNER)
    print(f" Status: {result['status']}")
    print(f" Attempts: {result.get('attempts', 0)}")
    if result.get("modified_files"):
        print(f" Modified files ({len(result['modified_files'])}):")
        for f in result["modified_files"]:
            print(f"  - {f}")
    v = result.get("result")
    if v is not None:
        errors = getattr(v, "errors", [])
        print(f" Remaining errors: {len(errors)}")
    if result.get("pr_url"):
        print(f" PR URL: {result['pr_url']}")

def _build_task_from_issue(issue_data: dict) -> str:
    """Construct a natural-language task description from a GitHub Issue.

    Args:
        issue_data: Dict with at least ``title``, ``body``, and ``html_url`` keys.

    Returns:
        A formatted prompt for the Agent.
    """
    title = issue_data.get("title", "Untitled Issue")
    body = issue_data.get("body", "(no description)")
    url = issue_data.get("html_url", "")

    lines = [
        f"Please fix the following GitHub Issue:",
        f"",
        f"Title: {title}",
        f"Issue URL: {url}",
        f"",
        f"Description:",
        f"{body}",
        f"",
        f"Analyze the root cause, locate the relevant code, generate a fix plan,",
        f"apply the changes, and ensure all tests pass after the fix.",
    ]
    return "\n".join(lines)


def _render_behavior_report(action_log: list) -> None:
    """Print a summary of what the Agent did during the run.

    Shows tool call counts, files edited/written, and notable bash commands.
    """
    if not action_log:
        return

    by_tool: dict[str, int] = {}
    edited_files: set[str] = set()
    written_files: set[str] = set()
    run_commands: list[str] = []
    read_files: int = 0

    for entry in action_log:
        tool = entry.get("tool", "?")
        by_tool[tool] = by_tool.get(tool, 0) + 1
        args = entry.get("args", {})
        if tool == "edit_file":
            fp = args.get("file_path", "")
            if fp:
                edited_files.add(str(fp))
        elif tool == "write_file":
            fp = args.get("file_path", "")
            if fp:
                written_files.add(str(fp))
        elif tool == "bash":
            cmd = args.get("command", "")
            if cmd and len(cmd) < 100:
                run_commands.append(cmd.strip())
        elif tool == "read_file":
            read_files += 1

    print()
    print(BANNER)
    print("  Agent Behavior Report")
    print(f"  Total tool calls: {sum(by_tool.values())}")
    parts = [f"    {tool}: {count}" for tool, count in sorted(by_tool.items())]
    for p in parts:
        print(p)
    if edited_files:
        print(f"  Files edited ({len(edited_files)}):")
        for f in sorted(edited_files):
            print(f"    - {f}")
    if written_files:
        print(f"  Files created ({len(written_files)}):")
        for f in sorted(written_files):
            print(f"    - {f}")
    if run_commands:
        print(f"  Bash commands ({len(run_commands)}):")
        for c in run_commands[:8]:
            short = c[:120] + ("..." if len(c) > 120 else "")
            print(f"    $ {short}")
    print(BANNER)


def main():
    args = _parse_args()
    config = Config.from_env()

    # ============ 分支 1：run（生产入口） ============
    if hasattr(args, "command") and args.command:
        if args.command == "run":
            repo_path = args.repo
            task = args.task

            # --issue 模式：从 GitHub API 获取 Issue 内容作为任务描述
            is_issue_task = bool(args.issue)
            if args.issue:
                if not repo_path:
                    print("  [FAIL] --repo is required when using --issue.")
                    return
                try:
                    from coderover.tools.github_client import GitHubClient, parse_issue_url
                    # Validate URL first (cheap, no network)
                    owner, repo_name, issue_num = parse_issue_url(args.issue)
                    print(f"  Fetching issue {owner}/{repo_name}#{issue_num} ...")
                    client = GitHubClient()
                    issue_data = client.get_issue(args.issue)
                    task = _build_task_from_issue(issue_data)
                    print(f"  Issue: {issue_data['title']} ({issue_data['state']})")
                    print(f"  Using repo: {repo_path}")
                except Exception as exc:
                    print(f"  [FAIL] Failed to fetch issue: {exc}")
                    return

            # Validate repo_path is provided
            if not repo_path:
                print("  [FAIL] --repo is required for run command.")
                return

            result = _run_harness_task(
                repo_path=repo_path,
                task=task,
                max_retries=args.max_retries,
                aggressive=args.aggressive,
                pr=args.pr,
                is_issue_task=is_issue_task,
                model=args.model,
                base_url=args.base_url,
                api_key=args.api_key,
            )
            if result.get("reason"):
                print(f"  [FAIL] {result['reason']}")
            elif args.json_output:
                # JSON output for CI integration
                json_payload = {
                    "status": result.get("status"),
                    "attempts": result.get("attempts", 0),
                    "modified_files": result.get("modified_files", []),
                    "skipped_low_severity": result.get("skipped_low_severity", 0),
                    "pr_url": result.get("pr_url"),
                }
                v = result.get("result")
                if v is not None:
                    errors = getattr(v, "errors", [])
                    json_payload["errors"] = [
                        {"tool": e.tool, "file": e.file, "line": e.line,
                         "error_type": e.error_type, "message": e.message[:120]}
                        for e in errors
                    ]
                print(json.dumps(json_payload, indent=2, default=str))
            else:
                status = result['status']
                usage = result.get("usage", {})
                elapsed = usage.get("elapsed_seconds", 0)
                pt = usage.get("prompt_tokens", 0)
                ct = usage.get("completion_tokens", 0)
                cost = usage.get("estimated_cost")

                print(BANNER)
                if status == "skipped":
                    print(f" Status: skipped — no issues found, no modifications needed")
                else:
                    print(f" Status: {status}")
                print(f" Attempts: {result.get('attempts', 0)}")
                print(f" Time: {elapsed}s")
                if pt or ct:
                    tokens_line = f" Tokens: {pt} prompt + {ct} completion = {pt + ct} total"
                    if cost is not None:
                        tokens_line += f"  (${cost:.4f})"
                    print(tokens_line)
                modified = result.get("modified_files", [])
                if modified:
                    print(f" Modified files ({len(modified)}):")
                    for f in modified:
                        print(f"  - {f}")
                v = result.get("result")
                if v is not None:
                    errors = getattr(v, "errors", [])
                    if errors:
                        print(f" Remaining errors: {len(errors)}")
                        for e in errors[:5]:
                            print(f"    [{e.tool}] {e.file}:{e.line}  {e.message[:80]}")
                if result.get("pr_url"):
                    print(f" PR: {result['pr_url']}")
                _render_behavior_report(result.get("action_log", []))
            return

        # ============ 分支 2：测试harness ============
        if args.command == "test-harness":
            _run_test_harness(args)
            return
    # ============   ============   ============

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, DEEPSEEK_API_KEY, or CORECODER_API_KEY\n"
            "\nExamples:\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    agent = Agent(llm=llm, max_context_tokens=config.max_context_tokens)

    # resume saved session
    if args.resume:
        loaded = load_session(args.resume)
        if loaded:
            agent.messages, loaded_model = loaded
            # restore the model from the saved session unless overridden by CLI
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    # one-shot mode
    if args.prompt:
        _run_once(agent, args.prompt)
        return

    # interactive REPL
    _repl(agent, config)


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    def on_token(tok):
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    agent.chat(prompt, on_token=on_token, on_tool=on_tool)
    print()


def _repl(agent: Agent, config: Config):
    """Interactive read-eval-print loop."""
    console.print(Panel(
        f"[bold]CodeRover[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))

    hist_path = os.path.expanduser("~/.coderover_history")
    history = FileHistory(hist_path)

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    while True:
        try:
            user_input = pt_prompt(
                "You > ",
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not user_input:
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(agent.messages)
            compressed = agent.context.maybe_compress(agent.messages, agent.llm)
            after = estimate_tokens(agent.messages)
            if compressed:
                console.print(f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages)[/green]")
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")
            continue
        if user_input == "/save":
            sid = save_session(agent.messages, config.model)
            console.print(f"[green]Session saved: {sid}[/green]")
            console.print(f"Resume with: coderover -r {sid}")
            continue
        if user_input == "/diff":
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/sessions":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                for s in sessions:
                    console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}) {s['preview']}")
            continue

        # call the agent
        streamed: list[str] = []

        def on_token(tok):
            streamed.append(tok)
            print(tok, end="", flush=True)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        try:
            response = agent.chat(user_input, on_token=on_token, on_tool=on_tool)
            if streamed:
                print()  # newline after streamed tokens
            else:
                # response wasn't streamed (came after tool calls)
                console.print(Markdown(response))
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /save          Save session to disk\n"
        "  /sessions      List saved sessions\n"
        "  quit           Exit CodeRover\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="CodeRover Help",
        border_style="dim",
    ))


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")

