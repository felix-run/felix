**A `policy_deny` audit row says which control refused the call.** `payload.control` is one of
`policy`, `limits`, `guardrails`, `approvals`, `command`, `screening`. Every wrapper stamped its
source on the deny it returned; the loop was the one reader that dropped it, so "show me every
call blocked by approvals" was unanswerable from the audit log until now.
