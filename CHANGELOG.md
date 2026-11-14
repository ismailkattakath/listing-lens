# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is the one in `pyproject.toml`.

## [Unreleased]

## [0.1.0] - 2026-11-13

### Added

- Initial public release: offline analysis of classified-listing crawls, as a
  scrape-harness consumer. The `llens` CLI and its pipeline (`pipeline.toml`),
  `llens refresh`, the record schemas, the derived Postgres export, the
  Grafana board tooling, and a synthetic test site (`tests/site`). Every site
  specific lives outside the repository.
- Community files (licence, code of conduct, contributing guide, security
  policy, issue and pull request templates) and CI: `gitleaks` over the whole
  history and `nix flake check`, behind one required `gate` check.

[Unreleased]: https://github.com/ismailkattakath/listing-lens/commits/main
[0.1.0]: https://github.com/ismailkattakath/listing-lens/commit/3943304
