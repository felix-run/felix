**A scheduled job can start each firing on a fresh thread.** `payload.fresh_thread: true` on a
`/jobs` row gives every run a thread of its own instead of the one thread per job name, so a job
that works a different ticket each time does not carry ticket N's transcript into ticket N+1's
context. The default is unchanged: a digest job keeps its history.
