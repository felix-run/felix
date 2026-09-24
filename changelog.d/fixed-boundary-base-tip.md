**The boundary check judges with the base branch's current script.** It checked out `base.sha`,
which is the base as of the pull request's last synchronize, so a parser fix on `main` never reached
an open Felix PR until its branch moved. It checks out `base.ref` now.
