**The contributor may rename and remove files in its checkout.** `write_file` only writes, so a
misnamed file was permanent: the second rung-2 run created a changelog fragment under the wrong
name and had no tool to fix it. `git mv` and `git rm` join the allowlist; both act inside the
checkout, and a commit is what makes either matter.
