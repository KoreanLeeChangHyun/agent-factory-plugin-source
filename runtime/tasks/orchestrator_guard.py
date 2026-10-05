#!/usr/bin/env python3
"""PreToolUse hook that keeps an orchestrator-mode Main from changing the project and a Codex Work
run from starting sub-agents.

Codex and Antigravity run this for tool calls. It is inert unless the provider process carries
AGENT_FACTORY_ORCHESTRATOR_GUARD, which the runtime sets only for orchestrate Main runs and Codex
Work runs; the variable's content selects the rules.
Main: reading, Agent Factory scripts, read-only Git and file writes inside the run directory.
Work: everything except the tools that start a sub-agent.
Standard library only: hooks start a fresh interpreter for every tool call.

Main's shell commands are allowed only when every part is understood: the guard splits the line as
the shell would, refuses expansions it cannot follow ($, globs or braces that could produce an
option) and accepts each listed command only with options that cannot write or execute.
"""
import json
import os
from pathlib import Path
import re
import shlex
import sys

ENV = "AGENT_FACTORY_ORCHESTRATOR_GUARD"
REASON = ("Orchestrator mode: Main may only read, write inside its run directory and run Agent Factory "
          "scripts. Shell commands must be plain read-only commands with read-only options (quote patterns; "
          "no $ expansion). Delegate project changes to Work; commits and direct edits need worker mode (direct).")
# Commands with no option that writes a file or runs a program; any arguments are allowed.
READ_COMMANDS = {"cat", "head", "tail", "ls", "pwd", "wc", "grep", "nl", "stat", "jq", "diff", "basename",
                 "dirname", "realpath", "readlink", "date", "echo", "printf", "true", "test", "cut", "tr",
                 "column", "which"}
# getopt-style allowlists (flags, flags taking a value, long options) for commands whose other options
# write or execute: long values are False (no value), True (required) or "optional" (only as --name=value).
# Long options must match exactly, so an abbreviation such as `--out=` for `--output` never passes.
OPTION_RULES = {
    "sort": ("bcCdfghiMmnRrsuVz", "kSt", {
        "--key": True, "--field-separator": True, "--buffer-size": True, "--parallel": True, "--sort": True,
        "--check": "optional", **dict.fromkeys((
            "--numeric-sort", "--general-numeric-sort", "--human-numeric-sort", "--version-sort", "--month-sort",
            "--reverse", "--unique", "--stable", "--ignore-case", "--ignore-leading-blanks", "--dictionary-order",
            "--ignore-nonprinting", "--random-sort", "--zero-terminated", "--merge", "--debug"), False)}),
    "uniq": ("cdDiuz", "fsw", {
        "--skip-fields": True, "--skip-chars": True, "--check-chars": True, "--all-repeated": "optional",
        "--group": "optional", **dict.fromkeys(("--count", "--repeated", "--ignore-case", "--unique",
                                                "--zero-terminated"), False)}),
    "tree": ("adlfxqNQugshDFvtcUriASnCXJ", "LPI", {
        "--charset": True, "--filelimit": True, "--sort": True, "--timefmt": True, **dict.fromkeys((
            "--gitignore", "--ignore-case", "--matchdirs", "--metafirst", "--prune", "--noreport", "--si", "--du",
            "--inodes", "--device", "--dirsfirst", "--filesfirst"), False)}),
    "file": ("0bcdEhiIkLlNnrsSvzZ", "efFmMP", {
        "--separator": True, "--files-from": True, "--magic-file": True, "--exclude": True, "--exclude-quiet": True,
        "--parameter": True, **dict.fromkeys((
            "--brief", "--mime", "--mime-type", "--mime-encoding", "--dereference", "--no-dereference",
            "--keep-going", "--raw", "--special-files", "--uncompress", "--uncompress-noreport", "--extension",
            "--apple", "--print0", "--no-pad", "--no-buffer"), False)}),
}
# A second `uniq` operand is its output file.
MAX_OPERANDS = {"uniq": 1}
# ripgrep has no option abbreviations; these two run a program.
RG_EXECUTES = ("--pre", "--hostname-bin")
GIT_READS = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "grep", "branch", "describe"}
# Git accepts unique abbreviations of long options, so any prefix of these is refused.
GIT_WRITES = ("--output", "--open-files-in-pager")
GIT_BRANCH = ("arvl", "", {
    "--format": True, "--sort": True, "--contains": True, "--no-contains": True, "--merged": True,
    "--no-merged": True, "--points-at": True, "--color": "optional", **dict.fromkeys((
        "--all", "--remotes", "--list", "--verbose", "--show-current", "--no-color", "--no-column",
        "--ignore-case"), False)})
FIND_TESTS = {"-H", "-L", "-P", "!", "-a", "-and", "-o", "-or", "-not", "-true", "-false", "-print", "-print0",
              "-ls", "-prune", "-quit", "-depth", "-follow", "-xdev", "-mount", "-noleaf", "-daystart",
              "-ignore_readdir_race", "-empty", "-readable", "-writable", "-executable", "-nouser", "-nogroup"}
FIND_VALUED = {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex",
               "-regextype", "-lname", "-ilname", "-type", "-xtype", "-maxdepth", "-mindepth", "-mtime", "-mmin",
               "-atime", "-amin", "-ctime", "-cmin", "-newer", "-anewer", "-cnewer", "-used", "-size", "-user",
               "-group", "-uid", "-gid", "-perm", "-links", "-inum", "-samefile", "-fstype", "-printf"}
SED_OPTIONS = ("nrEsuz", "e", {"--expression": True, **dict.fromkeys((
    "--quiet", "--silent", "--regexp-extended", "--separate", "--unbuffered", "--null-data", "--posix", "--debug",
    "--sandbox"), False)})
# sed commands that only select, print, edit the pattern/hold space or quit: no r/R/w/W/e or text commands.
SED_COMMANDS = set("pPl=dDnNgGhHxzqQF{}")
SED_DELIMITERS = set("/|#,:@!%;+=~_")
SED_ADDRESS = re.compile(r"\d+(?:~\d+)?|\$|[+~]\d+")
GLOB = "*?["
# Bash pairs braces loosely, so any unquoted { ... , or .. ... } counts as a brace expansion.
BRACE = re.compile(r"\{.*(?:,|\.\.).*\}")
# Antigravity tools that only read or report.
# Web search and URL reads are research, which orchestrator Main delegates.
AGY_READS = {"view_file", "list_dir", "find_by_name", "grep_search", "finish"}
AGY_WRITES = {"write_to_file", "replace_file_content", "multi_replace_file_content", "notebook_edit"}
PATH_ARGUMENTS = ("TargetFile", "AbsolutePath", "FilePath", "Path", "NotebookPath")
# Codex tools that start a sub-agent or reopen a closed one; hook tool names may carry a namespace prefix.
SUBAGENT_TOOLS = ("spawn_agent", "resume_agent")
# Codex 0.159 lets the model choose no sub-agent type and every sub-agent inherits the parent's
# sandbox, so none is a read-only exploration agent: Work may start none.
WORK_REASON = ("Codex Work cannot start sub-agents: Codex has no read-only exploration sub-agent. Do the "
               "search and the work yourself; never use a sub-agent to review or verify this run's own work.")
WORK_ENVIRONMENT = {ENV: json.dumps({"role": "work"})}


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
# A fixed interpreter name keeps the hook definition (and its Codex trust hash) stable across hosts.
HOOK_COMMAND = "python3 " + shlex.quote(str(Path(__file__).resolve()))


def orchestrating(state, session=None):
    """True for an orchestrate-mode Main run, which the guard keeps from changing the project."""
    role = state.get("role") or (session or {}).get("role")
    return role == "main" and state.get("taskMode") == "orchestrate"


def working(state, session=None):
    """True for a Work run, whose Codex launch the guard keeps from starting sub-agents."""
    return (state.get("role") or (session or {}).get("role")) == "work"


def plugin_roots():
    """This runtime's plugin copy and the Agent Factory copies Codex installed, which Codex binds as <plugin-root>.

    Fixed when the run is armed, so a directory Main shapes like a plugin later is never one."""
    codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    copies = sorted(root for root in (codex / "plugins" / "cache").glob("*/agent-factory/*")
                    if (root / "skills" / "agent" / "SKILL.md").is_file() and (root / "scripts" / "exec.py").is_file())
    return list(dict.fromkeys(os.path.realpath(root) for root in (PLUGIN_ROOT, *copies)))


def environment(state):
    """Provider-process variables that arm the guard for this orchestrate Main run."""
    return {ENV: json.dumps({"pluginRoots": plugin_roots(), "writeRoot": str(Path(state["statePath"]).parent)},
                            sort_keys=True)}


def inside(path, root, cwd):
    candidate = Path(path) if Path(path).is_absolute() else Path(cwd or ".") / path
    try:
        Path(os.path.realpath(candidate)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


def plugin_script(path, config, cwd):
    """A `scripts/*.py` file directly inside one of the plugin roots fixed when the run was armed."""
    candidate = Path(os.path.realpath(Path(cwd or ".") / path))
    return candidate.suffix == ".py" and any(candidate.parent == Path(os.path.realpath(root)) / "scripts"
                                             for root in config["pluginRoots"])


def split(command):
    """Split a command line as the shell would, or None when it uses an expansion the guard cannot follow.

    Words are (text, unquoted) pairs: `unquoted` keeps the characters the shell could still expand and
    turns quoted or escaped ones into NUL. Operator runs such as `|` or `&&` are (text, None)."""
    if "\n" in command:
        return None  # A newline starts another command, and a backslash before one joins two words.
    words, text, unquoted, quote, started, index = [], "", "", None, False, 0
    while index < len(command):
        character = command[index]
        if character in "$`" and quote != "'":
            return None  # Parameter, command and arithmetic expansion, also inside double quotes.
        if quote and character != quote:
            if quote == '"' and character == "\\" and command[index + 1:index + 2] in ('"', "\\"):
                index += 1
            text, unquoted = text + command[index], unquoted + "\0"
        elif quote:
            quote = None
        elif character == "\\":
            if index + 1 == len(command):
                return None
            index += 1
            text, unquoted, started = text + command[index], unquoted + "\0", True
        elif character in "'\"":
            quote, started = character, True
        elif character.isspace() or character in ";&|<>()":
            if started:
                words.append((text, unquoted))
                text, unquoted, started = "", "", False
            if not character.isspace():
                end = index
                while end < len(command) and command[end] in ";&|<>()":
                    end += 1
                words.append((command[index:end], None))
                index = end - 1
        elif character == "#" and not started:
            return None  # A comment would hide the rest of the line from the shell, not from the guard.
        else:
            text, unquoted, started = text + character, unquoted + character, True
        index += 1
    if quote:
        return None
    if started:
        words.append((text, unquoted))
    return words


def expands(text, unquoted):
    """True when the shell could turn this word into another word, such as an option the guard never saw.

    A glob that starts the word or sits in an option, or any brace expansion, can produce a name that
    begins with `-` (for example a run-directory file called `--pre=bash`)."""
    return unquoted[:1] in tuple(GLOB) or (text.startswith("-") and any(c in unquoted for c in GLOB)) \
        or bool(BRACE.search(unquoted))


def options(arguments, flags, valued, longs):
    """GNU getopt-style scan of arguments against allowed options: ([(option, value)], operands) or None."""
    found, operands, words = [], [], iter(arguments)
    for word in words:
        if word == "--":
            operands += list(words)
        elif word.startswith("--"):
            name, equals, value = word.partition("=")
            if name not in longs or (equals and longs[name] is False):
                return None
            if longs[name] is True and not equals:
                value = next(words, None)
                if value is None:
                    return None
            found.append((name, value if equals or longs[name] is True else None))
        elif word.startswith("-") and word != "-":
            for index, letter in enumerate(word[1:], 2):
                if letter in valued:
                    value = word[index:] or next(words, None)
                    if value is None:
                        return None
                    found.append(("-" + letter, value))
                    break
                if letter not in flags:
                    return None
                found.append(("-" + letter, None))
        else:
            operands.append(word)
    return found, operands


def git_read(command, arguments):
    if command not in GIT_READS:
        return False
    if command == "branch":
        # Branch names create branches; only listing options, and patterns after --list.
        parsed = options(arguments, *GIT_BRANCH)
        return parsed is not None and (not parsed[1] or any(o in ("-l", "--list") for o, _ in parsed[0]))
    for argument in arguments:
        if argument == "--":
            break
        name = argument.partition("=")[0]
        if name.startswith("--") and len(name) > 2 and any(option.startswith(name) for option in GIT_WRITES):
            return False
        if command == "grep" and argument.startswith("-") and not name.startswith("--") and "O" in argument:
            return False  # -O<pager> opens the matches in a program, also inside a short-option cluster.
    return True


def find_read(arguments):
    """find with tests and printing actions only: no -exec/-ok, -delete or -fprint/-fls output files."""
    words = iter(arguments)
    for word in words:
        if word in FIND_VALUED:
            if next(words, None) is None:
                return False
        elif word.startswith("-") and word not in FIND_TESTS:
            return False
    return True


def sed_part(script, index, delimiter, regex):
    """Index after one delimited sed part that starts at index, or None.

    GNU sed treats a delimiter inside a regex bracket expression as a literal; a bracket holding the
    delimiter or a backslash is refused so the guard and sed always end the part at the same place."""
    while index < len(script):
        character = script[index]
        if character == delimiter:
            return index + 1
        if character == "\\":
            index += 2
        elif character == "[" and regex:
            index += 1
            index += script[index:index + 1] == "^"
            index += script[index:index + 1] == "]"
            while index < len(script) and script[index] != "]":
                if script[index] in (delimiter, "\\"):
                    return None
                if script[index:index + 2] in ("[:", "[.", "[="):
                    close = script.find(script[index + 1] + "]", index + 2)
                    if close < 0 or delimiter in script[index:close] or "\\" in script[index:close]:
                        return None
                    index = close + 1
                index += 1
            index += 1
        else:
            index += 1
    return None


def sed_address(script, index):
    """Index after an optional sed address (number, $, step, /regex/ or \\cregexc), or None."""
    match = SED_ADDRESS.match(script, index)
    if match:
        return match.end()
    if script[index:index + 1] == "/" or script[index:index + 1] == "\\":
        index += script[index] == "\\"
        delimiter = script[index:index + 1]
        if delimiter not in SED_DELIMITERS:
            return None
        index = sed_part(script, index + 1, delimiter, True)
        while index is not None and script[index:index + 1] in ("I", "M"):
            index += 1
    return index


def sed_script(script):
    """True when a sed script only selects, prints, substitutes or quits: no command or `s` flag that
    reads, writes or executes (r, R, w, W, e, s///w, s///e)."""
    index, size = 0, len(script)
    while True:
        while index < size and script[index] in " \t;":
            index += 1
        if index == size:
            return True
        index = sed_address(script, index)
        if index is not None and script[index:index + 1] == ",":
            index = sed_address(script, index + 1)
        if index is None:
            return False
        while index < size and script[index] in " \t!":
            index += 1
        command = script[index:index + 1]
        index += 1
        if command in ("s", "y"):
            delimiter = script[index:index + 1]
            if delimiter not in SED_DELIMITERS:
                return False
            index = sed_part(script, index + 1, delimiter, command == "s")
            index = None if index is None else sed_part(script, index, delimiter, False)
            if index is None:
                return False
            while command == "s" and index < size and script[index] in "gpiImM0123456789":
                index += 1
        elif command in SED_COMMANDS:
            while command in "lqQ" and index < size and (script[index].isdigit() or script[index] == " "):
                index += 1
        else:
            return False
        while index < size and script[index] in " \t":
            index += 1
        if command != "{" and index < size and script[index] not in ";}":
            return False


def sed_read(arguments):
    parsed = options(arguments, *SED_OPTIONS)
    if parsed is None:
        return False
    scripts = [value for option, value in parsed[0] if option in ("-e", "--expression")] or parsed[1][:1]
    return bool(scripts) and all(sed_script(script) for script in scripts)


def allowed_segment(words, config, cwd, alone):
    if not words or "=" in words[0] or "/" in words[0]:
        return False  # Environment prefixes or a path could change which program runs.
    name, arguments = words[0], words[1:]
    if name in ("python3", "python"):
        # Only a whole command, so no pipe can feed a script's output onward.
        return alone and bool(arguments) and plugin_script(arguments[0], config, cwd)
    if name in ("bash", "sh", "zsh") and len(arguments) == 2 and arguments[0] in ("-c", "-lc"):
        return allowed_command(arguments[1], config, cwd, alone)
    if name == "git":
        return bool(arguments) and git_read(arguments[0], arguments[1:])
    if name == "find":
        return find_read(arguments)
    if name == "sed":
        return sed_read(arguments)
    if name == "rg":
        return not any(argument.partition("=")[0] in RG_EXECUTES for argument in arguments)
    if name == "command":
        # Only the lookup forms; `command <program>` runs any program.
        return (len(arguments) > 1 and arguments[0] in ("-v", "-V")
                and not any(argument.startswith("-") for argument in arguments[1:]))
    if name in OPTION_RULES:
        parsed = options(arguments, *OPTION_RULES[name])
        return parsed is not None and len(parsed[1]) <= MAX_OPERANDS.get(name, len(parsed[1]))
    return name in READ_COMMANDS


def allowed_command(command, config, cwd, alone=True):
    words = split(command)
    if words is None:
        return False
    segments, current = [], []
    for text, unquoted in words:
        if unquoted is None:
            if text != "|":
                return False  # Lists, redirections, background jobs and subshells.
            segments.append(current)
            current = []
        elif expands(text, unquoted):
            return False
        else:
            current.append(text)
    segments.append(current)
    return all(allowed_segment(segment, config, cwd, alone and len(segments) == 1) for segment in segments)


def patch_paths(patch):
    for line in str(patch).splitlines():
        for prefix in ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "):
            if line.startswith(prefix):
                yield line[len(prefix):].strip()


def codex_decision(event, config):
    tool, arguments = event.get("tool_name"), event.get("tool_input") or {}
    cwd = event.get("cwd")
    if tool == "Bash":
        return allowed_command(str(arguments.get("command", "")), config, cwd)
    if tool == "apply_patch":
        paths = list(patch_paths(arguments.get("command", "")))
        return bool(paths) and all(inside(path, config["writeRoot"], cwd) for path in paths)
    return True


def work_decision(event):
    tool = event.get("tool_name")
    return not (isinstance(tool, str) and any(name in tool for name in SUBAGENT_TOOLS))


def agy_decision(event, config):
    call = event.get("toolCall") or {}
    tool, arguments = call.get("name"), call.get("args") or {}
    cwd = arguments.get("Cwd") or next(iter(event.get("workspacePaths") or []), None)
    if tool in AGY_READS:
        return True
    if tool == "run_command":
        return allowed_command(str(arguments.get("CommandLine", "")), config, cwd)
    if tool in AGY_WRITES:
        paths = [arguments[key] for key in PATH_ARGUMENTS if isinstance(arguments.get(key), str)]
        return bool(paths) and all(inside(path, config["writeRoot"], cwd) for path in paths)
    return False


def main():
    event = json.load(sys.stdin)
    agy = "toolCall" in event
    raw = os.environ.get(ENV)
    reason = REASON
    if not raw:
        allowed = True
    else:
        try:
            config = json.loads(raw)
            if config.get("role") == "work":
                allowed, reason = work_decision(event), WORK_REASON
            else:
                allowed = agy_decision(event, config) if agy else codex_decision(event, config)
        except Exception:
            allowed = False  # Fail closed while guarding.
    if agy:
        print(json.dumps({"decision": "allow" if allowed else "deny", **({} if allowed else {"reason": reason})}))
    elif not allowed:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
