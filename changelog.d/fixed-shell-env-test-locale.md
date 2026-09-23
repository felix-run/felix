**The shell tool's environment test passes inside the image.** CPython coerces a C locale to
`LC_CTYPE=C.UTF-8` in the child (PEP 538), so the test that pins the five-variable environment
failed in the builder container while passing on macOS and CI. It now tolerates that injection as
it already did macOS's `__CF_USER_TEXT_ENCODING`. Found by Felix's first rung-2 run.
