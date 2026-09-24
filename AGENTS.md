# cook-studio rules

- Support only DeepSeek V4 Flash and Qwen 3.8 Flash Next through native DS4 Metal.
- Keep README.md short. Put local plans and operational notes in ignored docs/.
- Keep commands in run.sh, implementation in src/, and pins/settings in config/.
- Keep DS4 Git links and model revisions consistent. Do not claim speed gains
  without matched measurements and quality checks on this Mac.
- Load one large model at a time. Never stop a server owned by another process.
- Keep weights, credentials, host-specific details, caches, benchmarks, and
  historical results out of Git. Use one ignored root .env for stack credentials.
- Preserve working authentication, chat data, and upstream licenses.
- Reuse .venv and installed tools. Run ./run.sh check after code/config changes.
  Engine changes also need targeted upstream tests and matched inference checks.
- Work on the current branch. Do not commit, push, rewrite Git history, or make
  system-wide changes without explicit authorization.
