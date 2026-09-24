**The scoreboard no longer counts an "update branch" merge as a human commit.** The first Felix
PR scored 0 % on "merged without human commits" because a person pressed GitHub's update-branch
button, which authors a merge commit with no change of its own. Merge commits are skipped.
