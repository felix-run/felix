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

# One shell segment per line: split on ; && || | and newlines.
#
# Guards ask "does this command do X", and the honest unit for that question is the
# segment, not the line. `git stash push -q f && rm -f /tmp/x` contains "push" and
# "-f " and is not a force-push; as two segments neither one looks like one.
hook_segments() {
  hook_executable_text | tr '\n' ';' | sed 's/&&/;/g; s/||/;/g; s/|/;/g' | tr ';' '\n' |
    sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -v '^$'
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
