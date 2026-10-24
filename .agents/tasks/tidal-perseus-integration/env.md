# Environment report: tidal-perseus-integration

## Pipeline (repo 1): /Users/michaellaschaszberger/Desktop/hackachusetts/pipeline
- venv present: `pipeline/.venv` (has python, pytest, ruff)
- Test: `cd '/Users/michaellaschaszberger/Desktop/hackachusetts/pipeline' && .venv/bin/python -m pytest -q` (testpaths=tests, addopts -ra)
- Lint: `cd '/Users/michaellaschaszberger/Desktop/hackachusetts/pipeline' && .venv/bin/ruff check .` (line-length 88, py311, rules E,F,I,B,UP,W)
- Entry point: `siting-run` = `siting.run:main`

## Perseus frontend (repo 2): '/Users/michaellaschaszberger/Downloads/perseus-night-watch 2'
- Build: `cd '/Users/michaellaschaszberger/Downloads/perseus-night-watch 2' && npm run build` (= `node build.mjs`, output dist/app.js, copies public/ and index.html into dist/)
- Dev/serve: `npm run dev` (= `node build.mjs --serve`, watch + http://localhost:5173); preview: `npm run preview` (`npx --yes serve dist`)
- node_modules NOT installed: `npm install` required before building (react ^19.2.0, react-dom ^19.2.0, esbuild ^0.28.0)

## Git status
- hackachusetts: git repo (main, HEAD 8e2a348, tracks origin/main). Untracked: .gitignore, .kiro/specs/, .kiro/steering/tidal-siting-data.md, config/, data/, docs/, frontend/, output/, pipeline/
- perseus-night-watch 2: NOT a git repo (do not initialize)

## Commit policy
No automatic commit hook flow: `.kiro/hooks/commit-message-helper.kiro.hook` is manual-only (suggests a message, asks user). Decision: do NOT commit in either repo; leave all changes uncommitted.
