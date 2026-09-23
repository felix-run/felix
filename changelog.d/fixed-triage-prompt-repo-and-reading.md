**The triage manifest names its repository and how to reach a line.** Two live runs taught it: one
guessed the GitHub owner from the manifest's name and got 422 on every search; another spent all
thirty steps paging a file by byte offsets to reach a line number that `search_files` would have
returned in one call. The prompt now says both.
