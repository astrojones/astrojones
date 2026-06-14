# Changelog

## [1.1.0](https://github.com/astrojones/raisl/compare/v1.0.2...v1.1.0) (2026-06-14)


### Features

* add SessionStart hook to initialize Serena with project directory ([aef26de](https://github.com/astrojones/raisl/commit/aef26de9fb40ea2f212b05a41362cf4535d6c566))


### Bug Fixes

* resolve Serena's repo root lazily at first call, not at server import ([c971144](https://github.com/astrojones/raisl/commit/c971144561bcb288e11f1251b477baf919b3ada0))

## [1.0.2](https://github.com/astrojones/raisl/compare/v1.0.1...v1.0.2) (2026-06-14)


### Bug Fixes

* **scaffold:** repoint harness install spec to raisl/servers/harness-mcp ([c6bf1ed](https://github.com/astrojones/raisl/commit/c6bf1ed4a41af898cc5939837f2412292a72b61a))

## [1.0.1](https://github.com/astrojones/raisl/compare/v1.0.0...v1.0.1) (2026-06-14)


### Bug Fixes

* **agents:** repair stale astrojones-dev MCP tool prefix in agent tool lists ([73328d3](https://github.com/astrojones/raisl/commit/73328d3f012f9c28a03b56ae126b7bbc4c0521d6))

## [1.0.0](https://github.com/astrojones/raisl/compare/v0.14.0...v1.0.0) (2026-06-14)


### ⚠ BREAKING CHANGES

* rename plugin to raisl and split out the org deploy layer
* workflow skills/agents move namespaces, e.g.
* **plugin:** re-point the marketplace: claude plugin marketplace remove astrojones && claude plugin marketplace add astrojones/claude-plugins

### Features

* absorb the repo-agent-harness Claude Code surface; bump to 0.6.0 ([0d2e03c](https://github.com/astrojones/raisl/commit/0d2e03ce06b0bc2751c4dd3f25335b728604680f))
* **agents:** add context-explorer; harden hook shim; bump to 0.7.0 ([af8f573](https://github.com/astrojones/raisl/commit/af8f573e461ed483613160c1dbd9ef668b7ef35c))
* **agents:** add fullstack-architect for UI⇄backend vertical slices ([b04c505](https://github.com/astrojones/raisl/commit/b04c505ea609fbcf8f33607eb196b49114b5a285))
* **agents:** make Serena-first preference structural via tool allowlists ([8fae3cf](https://github.com/astrojones/raisl/commit/8fae3cff2158a54ea87b1bccc1803a9c131fb39f))
* born-harnessed scaffolds — repo-carried deploy tools + harness composition ([d516d35](https://github.com/astrojones/raisl/commit/d516d35e13652f302f9ee97b3f06de659ff5839e))
* **harness-mcp:** add bootstrap CLI subcommand + repo_bootstrap_status MCP tool ([0daaea6](https://github.com/astrojones/raisl/commit/0daaea6e30063adb3c0ddfe8f7edbf032a34d4f6))
* **harness-mcp:** add prompt-drift detection (warning, never error) ([719eab8](https://github.com/astrojones/raisl/commit/719eab8c75695f52d4c03c3f672dc1bc1ba76247))
* **harness-mcp:** bundle repo-agent-harness server in plugin ([a7ae34a](https://github.com/astrojones/raisl/commit/a7ae34acc46ae4acb28a104504c63585917589fa))
* **harness-mcp:** expose deploy validators as MCP tools + CLI subcommands ([f34ed9d](https://github.com/astrojones/raisl/commit/f34ed9d2a693c3bcc974cb8ae4fc5d3db7726728))
* **harness-mcp:** expose per-repo workflow prompts as the SSOT ([f7099c5](https://github.com/astrojones/raisl/commit/f7099c5243ee2bbcb874d86ca0d0d1a0a70bb4bf))
* **harness-mcp:** surface workflow orientation to any MCP client ([26140fd](https://github.com/astrojones/raisl/commit/26140fd008f64fd7548c96b044c8d8982fc046ac))
* initial astrojones-dev plugin (nuklaut-deploy skill, /new-app, deploy-doctor) ([fa917a6](https://github.com/astrojones/raisl/commit/fa917a6021d8fb8f06b2bd14c98a7ec90d48a243))
* **opencode:** dual-target plugin half — Plugin factory + surfaces ([62179fe](https://github.com/astrojones/raisl/commit/62179fe5bbb2e3fa00b5cc36710d9c4d09b6f4fe))
* **opencode:** wire permission.ask confirm-first tier; broaden destructive rm regex; fix YAML frontmatter ([c6bad84](https://github.com/astrojones/raisl/commit/c6bad84bad6e904aea9d0dbb898cb9ffc16d8384))
* python-backend scaffold (standards-wired, gate-verified), admin ref, stack-select /new-app ([b7f55ba](https://github.com/astrojones/raisl/commit/b7f55bac5852d51b4c0b8117ce87d50da0958b2a))
* register repo-agent-harness in astrojones marketplace ([84e5c68](https://github.com/astrojones/raisl/commit/84e5c68a0006035867fc484c306672101dfc10ce))
* release-please automation + rename commit-semantic workflow to commit ([3b0a1b0](https://github.com/astrojones/raisl/commit/3b0a1b01611113d2145503561931d04e5a74530a))
* rename plugin to raisl and split out the org deploy layer ([04d1f03](https://github.com/astrojones/raisl/commit/04d1f0345398d7a430162d97ad565fa1ea05ead9))
* **skills:** bundle pyproject-canon, closing the /new-app gap ([e240abf](https://github.com/astrojones/raisl/commit/e240abf2a1e2be3feaa9f2a6853b9eed383e8276))


### Bug Fixes

* /new-app template copy includes dotfiles; Dockerfile copies README.md ([0597008](https://github.com/astrojones/raisl/commit/0597008f4ce9cb3f0df00472d87427a0f4f0e8d7))
* **gateway:** pass repo root as serena --project, not state dir ([f1c2dfa](https://github.com/astrojones/raisl/commit/f1c2dfae448f5a28832ce3fae3f585d39905ddd2))
* **harness:** auto-bootstrap must fail open on any exception, not just OSError ([bb7abf0](https://github.com/astrojones/raisl/commit/bb7abf0a97591344a6cc13ecc3e6e4f337d074ea))
* **mcp:** restore plugin .mcp.json declaring bundled harness server ([274b552](https://github.com/astrojones/raisl/commit/274b552f50093b6c4e06aa4ffcd2db716ddad62b))
* **opencode:** fail closed on harness confirm-first tier ([77d9909](https://github.com/astrojones/raisl/commit/77d9909004822d08e8bfdeb2f9362343bfdefe82))
* **scaffold:** align init subcommand to install agent/ and opt-in AGENTS.md ([c5ef520](https://github.com/astrojones/raisl/commit/c5ef52041a77ea435a0890b879abb77c32a22f24))
* **scaffold:** clarify init_repo docstring and default behavior ([9dae900](https://github.com/astrojones/raisl/commit/9dae900150e3d5d3d470399628d04ae512717f9c))
* **scaffold:** converge opencode skills.paths across re-bootstraps ([4de54fb](https://github.com/astrojones/raisl/commit/4de54fbebdb8e3664c4f7142b247defa4b641eb7))


### Code Refactoring

* **plugin:** move the org marketplace to astrojones/claude-plugins; bump to 0.5.0 ([7e80079](https://github.com/astrojones/raisl/commit/7e8007942e572888e6ff9e3930cb1c08bacfc425))
