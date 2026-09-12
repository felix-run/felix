# Shared by the PreToolUse(Bash) guards. Source it; it defines functions only.
#
# Every guard here started by matching a substring of the whole command, and every one
# of them fired on text that was never going to execute: a word inside a heredoc, a
# quoted commit message, a filename, an unrelated command later in the same line. In
# one session that produced seven false blocks, including a guard refusing to let its
# own source file be read because the filename contained the word it watches for, and
# `git-guard` blocking `git commit -m 'do not push --force here'` -- its own advice.
#
# The fix is to match what will run rather than what the string contains. These helpers
# do the two things that requires: drop heredoc bodies, which are data, and split the
# rest into the segments a shell would actually execute.

# Everything the shell will run, with heredoc bodies removed.
#
# A heredoc body is input to a command, not a command. `cat <<'EOF' … EOF` carrying a
# PR description or a Python script is the single most common source of a false match,
# because prose and code mention the very commands these guards watch for.
hook_executable_text() {
  awk '
    # Opening delimiter: <<WORD, <<-WORD, <<"WORD", <<'"'"'WORD'"'"'. Take the last one
    # on the line -- `cmd <<A | cmd <<B` is legal -- and swallow until it appears alone.
    !inbody && match($0, /<<-?[[:space:]]*["'"'"']?[A-Za-z_][A-Za-z0-9_]*["'"'"']?/) {
      d = substr($0, RSTART, RLENGTH)
      gsub(/^<<-?[[:space:]]*|["'"'"']/, "", d)
      inbody = 1; delim = d
      print; next
    }
    inbody {
      line = $0
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
      if (line == delim) inbody = 0
      next
    }
    { print }
  '
}

# One shell segment per line: split on ; && || | and newlines, outside quotes.
#
# Guards ask "does this command do X", and the honest unit for that question is the
# segment, not the line. `git stash push -q f && rm -f /tmp/x` contains "push" and
# "-f " and is not a force-push; as two segments neither one looks like one.
#
# Quote-aware, because a blind `sed s/|/;/g` also split inside quoted arguments. An
# alternation is the everyday case: `grep -nE '(pip|python|PYTEST)=' ~/.zshrc` became a
# segment starting with the watched word, so the test guard blocked a grep -- and then
# blocked the attempt to investigate itself. `hook_words` already applies the shell's
# quoting rules via xargs; this is the same correction one level up.
#
# The quote and escape characters are built with sprintf rather than written, so this awk
# program contains no quote or backslash of its own and needs no shell escaping dance.
# The previous attempt at this function died on exactly that.
hook_segments() {
  hook_executable_text | awk '
    BEGIN { SQ = sprintf("%c", 39); DQ = sprintf("%c", 34); BS = sprintf("%c", 92) }
    function flush(   s) {
      s = seg
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      if (s != "") print s
      seg = ""
    }
    {
      n = length($0)
      for (i = 1; i <= n; i++) {
        c = substr($0, i, 1)
        if (esc)              { seg = seg c; esc = 0; continue }
        if (c == BS && !insq) { seg = seg c; esc = 1; continue }
        if (c == SQ && !indq) { insq = !insq; seg = seg c; continue }
        if (c == DQ && !insq) { indq = !indq; seg = seg c; continue }
        if (!insq && !indq) {
          if (c == ";") { flush(); continue }
          if (c == "|") { if (substr($0, i + 1, 1) == "|") i++; flush(); continue }
          if (c == "&" && substr($0, i + 1, 1) == "&") { i++; flush(); continue }
        }
        seg = seg c
      }
      # An unterminated quote means the segment continues on the next line, as it would
      # in a shell; only an unquoted end-of-line ends a segment.
      if (insq || indq) seg = seg sprintf("%c", 10)
      else flush()
    }
    END { flush() }
  '
}

# One word per line, honouring shell quoting.
#
# Segment-level matching still read `git commit -m 'do not push --force here'` as a
# force-push, because the flag was inside a quoted argument. `xargs` applies the shell's
# own word-splitting rules without running anything, so that message becomes a single
# token that matches no flag. Unbalanced quotes make xargs fail; fall back to a plain
# split, which is the pre-existing behaviour and errs toward matching.
hook_words() {
  printf '%s\n' "$1" | xargs -n1 2>/dev/null || printf '%s\n' "$1" | tr ' \t' '\n'
}

# True when the segment passes an exact flag, as a word rather than as text.
hook_has_flag() {
  local seg=$1 flag
  shift
  while IFS= read -r word; do
    for flag in "$@"; do
      [ "$word" = "$flag" ] && return 0
    done
  done <<WORDS
$(hook_words "$seg")
WORDS
  return 1
}

# The subcommand of a segment, given the verb `hook_segment_verb` already resolved.
# `git -C /path -c k=v push` is a push; `git commit -m "... push ..."` is not; and
# `env git push` is one too, which is why the scan starts at the verb rather than at
# word two.
hook_subcommand() {
  hook_words "$1" | awk -v verb="$2" '
    !seen { if ($0 == verb || $0 ~ "/" verb "$") seen = 1; next }
    skip { skip = 0; next }
    /^-C$|^-c$|^--git-dir$|^--work-tree$|^--namespace$|^--exec-path$/ { skip = 1; next }
    /^-/ { next }
    { print; exit }
  '
}

# The words a segment actually begins with, after env assignments and simple runners.
#
# `uv run pytest -q`, `FOO=1 pytest`, `command git push` and `pytest` should all answer
# "pytest"/"git"; `cat notes-about-pytest.md` should not.
hook_segment_verb() {
  printf '%s\n' "$1" | awk '{
    i = 1
    while (i <= NF && ($i ~ /^[A-Za-z_][A-Za-z0-9_]*=/ ||
                       $i == "env" || $i == "command" || $i == "exec" ||
                       $i == "sudo" || $i == "time" || $i == "nohup")) i++
    # One layer of runner: `uv run X`, `poetry run X`, `npx X`, `python -m X`.
    if (($i == "uv" || $i == "poetry" || $i == "pdm" || $i == "hatch") && $(i+1) == "run") i += 2
    else if ($i == "npx" || $i == "pnpm" || $i == "bunx") i += 1
    else if ($i ~ /^python[0-9.]*$/ && $(i+1) == "-m") i += 2
    if (i <= NF) { sub(/^.*\//, "", $i); print $i }
  }'
}

# The value of a global `git -C <path>`, if the segment passes one.
#
# Global options come before the subcommand, and only there does `-C` mean a directory:
# `git commit -C HEAD` reuses a commit message, and reading that as a path would point a
# guard at a directory that does not exist and silently answer "not on main". So the scan
# starts at the verb, walks the global options the way `hook_subcommand` does, and stops
# the moment the subcommand appears.
hook_git_dir_flag() {
  hook_words "$1" | awk '
    !seen { if ($0 == "git" || $0 ~ /\/git$/) seen = 1; next }
    take { print; exit }
    skip { skip = 0; next }
    /^-C$/ { take = 1; next }
    /^-c$|^--git-dir$|^--work-tree$|^--namespace$|^--exec-path$/ { skip = 1; next }
    /^-/ { next }
    { exit }
  '
}

# Which directory will the command actually run in?
#
# Not necessarily the one the hook was invoked from, and not `CLAUDE_PROJECT_DIR` either:
# a session in a git worktree, or one that opens a PR in a sibling checkout with a leading
# `cd`, runs its commands somewhere the project root cannot tell you about. A guard that
# asks the wrong directory is worse than absent -- `git-guard` read the main checkout's
# branch for a session working in a worktree, so it nagged "you are about to commit on
# main" at every commit on a feature branch, and refused a legitimate `--force-with-lease`
# on one. A guard that cries wolf gets worked around.
#
# Takes the raw hook payload and the command. The payload's `cwd` is the starting point;
# `cd`s on the first line move it, in order, keeping the last one that exists -- which is
# what bash does for a `&&` or `;` chain, and makes relative chains fall out for free.
#
# First line only: `^` in sed anchors per line, so scanning the whole command follows a
# `cd` inside a heredoc, where "cd /tmp/repro" is ordinary reproduction prose. A guard
# that redirects itself depending on whether a path named in a PR description happens to
# exist locally is harder to notice than one that is plainly broken.
hook_workdir() {
  local input=$1 cmd=$2 workdir target first
  workdir=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
  # Fallbacks, in order, for a payload that carries no cwd: the project root, then the
  # hook's own directory. The project root before `pwd`, because a hook's process cwd is
  # whatever it inherited -- deciding a branch rule on that makes the answer depend on
  # where the process happened to start, which is the class of bug this function exists
  # to end rather than relocate.
  [ -n "$workdir" ] || workdir=${CLAUDE_PROJECT_DIR:-}
  [ -n "$workdir" ] || workdir=$(pwd -P)
  first=$(printf '%s' "$cmd" | head -n 1)
  while IFS= read -r target; do
    [ -n "$target" ] || continue
    target=${target%"${target##*[![:space:]]}"}   # trailing whitespace
    # One matched pair, peeled by hand -- a hook must never eval command text. Peeling
    # both pairs unconditionally resolved `cd "'/path'"` to /path, which is not where
    # bash goes: bash fails that cd and stays put. The hook and the shell disagreeing
    # about which directory a command runs in is the bypass, not the quoting itself.
    case "$target" in
      \"*\") target=${target#\"}; target=${target%\"} ;;
      \'*\') target=${target#\'}; target=${target%\'} ;;
    esac
    case "$target" in "~") target="$HOME" ;; "~/"*) target="$HOME/${target#\~/}" ;; esac
    case "$target" in /*) ;; *) target="$workdir/$target" ;; esac
    [ -d "$target" ] && workdir=$target
  done <<TARGETS
$(printf '%s' "$first" | tr ';&|' '\n\n\n' | sed -n 's/^[[:space:]]*cd[[:space:]]\{1,\}\(.*\)$/\1/p')
TARGETS
  printf '%s\n' "$workdir"
}
