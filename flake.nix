# listing-lens — the harness, not the model, drives. A CONSUMER of scrape-harness since phase 3:
# the generic core (CLI, crawl, approvals, store, contracts, the Claude Code plugin) is the
# `scrape-harness` flake input; this repo holds only its own source, analysis code, record schemas
# and pipeline.toml.
#
#   nix develop          → python (harness + deps, editable listing_lens), the `llens` CLI, ruff, check-jsonschema, gitleaks
#   nix run              → the `llens` CLI (packages.default; pipeline.toml is its default pipeline)
#   nix run .#llens -- … → the same CLI with this checkout's .env* and .envrc.local loaded (harness app)
#   nix run .#preflight -- refresh → what a refresh needs (secrets by NAME, signers, cooldown), offline
#   nix run .#cli-spec   → the command tree as cli.v1 JSONL (repo-only)
#   nix run .#env-doctor → which catalogued env vars are set (NAMES only, never values)
#   nix run .#grafana-admin perms|ds-password|bootstrap → what stays outside the stack (RBAC, the DS password, the SA)
#   nix run .#grafana-tf plan|apply → the same objects as an encrypted OpenTofu stack (state under DATA)
#   nix run .#leak-gate  → gitleaks + forbidden-literal grep over unpushed commits
#   nix run .#ship       → THE push (the harness's ship): clean main → signed → leak-gate → flake check →
#                          darwin tests rebuilt → linux package → live Claude Code smoke → push
#   nix fmt              → treefmt (nixfmt + ruff format)
#   nix flake check      → package build (runs pytest, incl. the switch goldens), ruff, schema gates,
#                          formatting, toolchain-complete
{
  description = "listing-lens: typed, append-only pipeline for studying public listing pages";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    scrape-harness = {
      url = "github:ismailkattakath/scrape-harness";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.treefmt-nix.follows = "treefmt-nix";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      treefmt-nix,
      scrape-harness,
    }:
    let
      inherit (nixpkgs) lib;
      harness = scrape-harness.lib;
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAll = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      pyproject = lib.importTOML ./pyproject.toml;
      # pipeline.toml [consumer] is the ONE place listing-lens' world-visible names live.
      consumer = (lib.importTOML ./pipeline.toml).consumer;

      src = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./pyproject.toml
          ./pipeline.toml
          ./src
          ./schemas
          ./tests
        ];
      };

      # ONE Python for the package and the dev shell, with ONE override (team-lead's option A, 2026-11-08).
      # WHY: at this nixpkgs pin cache.nixos.org has NO spacy on either system, even unmodified. Hydra's chain
      # spacy -> spacy-loggers -> wandb -> moviepy -> imageio -> (pillow-heif on darwin, astropy on linux)
      # fails (measured with `nix path-info --store https://cache.nixos.org`). nixpkgs makes wandb a HARD
      # dependency of spacy-loggers; upstream it is optional (only its W&B logger imports it), and llens runs a
      # blank pipeline with no training loggers. So the override drops that one edge; spaCy then builds here.
      # RETIRE: delete this override once `nix path-info --store https://cache.nixos.org` finds nixpkgs'
      # python3Packages.spacy outPath for BOTH aarch64-darwin and aarch64-linux at the pinned nixpkgs.
      # CI pays the spaCy build per run (no push to any binary cache: this is a persona repo).
      pythonFor =
        pkgs:
        pkgs.python3.override {
          packageOverrides = _: super: {
            spacy-loggers = super.spacy-loggers.overridePythonAttrs (o: {
              dependencies = builtins.filter (d: (d.pname or "") != "wandb") (o.dependencies or [ ]);
            });
            # services-match@2's stock lookup lemmatizer table (MIT, 1.0.5; ~99 MB: it ships every language).
            # WHY: at this pin only its CHECK phase fails: the test deps pull imageio/moviepy -> pillow-heif, the
            # same broken chain as above; the package itself is data. So its tests are skipped, nothing else.
            # RETIRE: drop this override once nixpkgs' spacy-lookups-data builds unmodified at the pin.
            spacy-lookups-data = super.spacy-lookups-data.overridePythonAttrs (_: {
              doCheck = false;
              nativeCheckInputs = [ ];
            });
          };
        };

      # listing-lens' own dependencies on top of the harness's.
      pyDepsFor =
        pkgs: ps:
        let
          vocab = import ./nix/vocab-python-packages.nix { inherit pkgs ps; };
        in
        [ (harness.python pkgs ps) ]
        ++ harness.pyDeps pkgs ps
        ++ [
          ps.geopy
          ps.psycopg
          # `llens services` (service vocabulary, plan v2): spaCy BLANK pipeline only, no model download
          ps.spacy
          ps.spacy-lookups-data # services-match@2: the en lemma_lookup table
          ps.ftfy
          ps.pyicu
          ps.emoji
          ps.rdflib
          # the region rule by census-division boundary (boundaries.py): read the site's boundary shapefile once,
          # reproject its source_crs -> WGS84, point-in-polygon with an edge buffer in metres
          ps.pyshp
          ps.pyproj
          ps.shapely
          vocab.negspacy
          vocab.abbreviation-extractor
        ];

      llens =
        pkgs:
        let
          ps = (pythonFor pkgs).pkgs;
        in
        ps.buildPythonApplication {
          pname = pyproject.project.name;
          inherit (pyproject.project) version;
          pyproject = true;
          inherit src;
          build-system = [ ps.hatchling ];
          dependencies = pyDepsFor pkgs ps;
          # pytest runs inside the build: the package cannot exist with a red test.
          nativeCheckInputs = [
            ps.pytestCheckHook
            ps.wcwidth
            pkgs.git
            pkgs.openssh
            (pkgs.postgresql.withPackages (p: [ p.postgis ])) # export-db's test server (with PostGIS), unix socket only
          ];
          # the switch goldens run the harness plugin's guard against this repo's pipeline.toml
          preCheck = ''
            export LLENS_REQUIRE_PG=1 # the export-db test must run here, never skip
            export SCRAPE_HARNESS_PLUGIN=${scrape-harness.packages.${pkgs.stdenv.hostPlatform.system}.plugin}
          '';
          makeWrapperArgs =
            harness.wrapperArgs {
              inherit pkgs consumer;
              rev = self.shortRev or self.dirtyShortRev or "unknown";
              pipeline = "${src}/pipeline.toml";
            }
            # the site's source loads from DATA (Drive): never write its bytecode there
            ++ [
              "--set"
              "PYTHONDONTWRITEBYTECODE"
              "1"
            ];
          meta.mainProgram = consumer.cli;
        };

      tools =
        pkgs:
        harness.consumerTools {
          inherit pkgs consumer;
          plugin = "${scrape-harness.packages.${pkgs.stdenv.hostPlatform.system}.plugin}";
          public = true; # a public repo: ship also runs public-gate
          # the env-loading apps (`nix run .#llens -- ...`, .#preflight, .#cli-spec) wrap the built CLI
          cliPackage = llens pkgs;
          # checks.claude-md-drift: the route block between the markers equals `llens claude-md`'s render
          claudeMd = ./CLAUDE.md;
          extraCatalogue = {
            LLENS_DB_URL = {
              need = "optional";
              secret = true;
              note = "DSN of the derived Postgres copy, only under `secret exec LLENS_DB_URL=<item> -- llens export-db`.";
            };
            GEOCODE_KEY = {
              need = "optional";
              secret = true;
              note = "Google Geocoding key, only under `secret exec GEOCODE_KEY=<item> -- llens geocode`.";
            };
            LLENS_GEOCODE_KEYCHAIN_ITEM = {
              need = "optional";
              note = "Keychain item holding the key (default llens:geocode:apikey); used in messages.";
            };
            GOOGLE_CLOUD_PROJECT = {
              need = "optional";
              note = "Project owning the key, for gcloud quota/key management. llens never reads it.";
            };
            LLENS_REQUIRE_GQM = {
              need = "optional";
              note = "1 = the GQM panel/question tests FAIL when DATA notes/li-grafana-gqm.md is unreadable instead of skipping; ship's local check sets it.";
            };
          }
          # the live systems' names: GENERIC defaults in the code, the live values bound outside the repo
          //
            lib.mapAttrs
              (_: note: {
                need = "optional";
                inherit note;
              })
              {
                LLENS_GRAFANA_URL = "The Grafana stack URL (grafana-admin, grafana-tf).";
                LLENS_GRAFANA_UID_PREFIX = "Every board uid is <prefix>-<board> (default llens).";
                LLENS_GRAFANA_DS_UID = "The datasource uid (default <prefix>-db); its tofu resource name follows it.";
                LLENS_GRAFANA_FOLDER_UID = "The folder uid (default <prefix>-private); its tofu resource name follows it.";
                LLENS_GRAFANA_TITLE_PREFIX = "Board titles are '<prefix> - <board>' (default Listing Lens).";
                LLENS_GRAFANA_FOLDER_TITLE = "The folder title (default '<title prefix> (private)').";
                LLENS_GRAFANA_TAG = "Every board's first tag (default listing-lens).";
                LLENS_GRAFANA_SA_NAME = "The boards' service account (bootstrap; default llens-boards).";
                LLENS_GRAFANA_SCHEMA = "v2 (default) or v1: the dashboard resource type the tofu stack renders.";
                LLENS_TOFU_KEY_NAME = "The tofu state encryption's key/method name; the state records it (default llens).";
                LLENS_GRAFANA_TOKEN_ITEM = "Keychain item of the boards' Grafana token (default llens:grafana:token).";
                LLENS_GRAFANA_ADMIN_TOKEN_ITEM = "Keychain item of an org-admin token, for `grafana-admin bootstrap` only.";
                LLENS_PG_DSN_ITEM = "Keychain item of the boards' read-only DSN (default llens:db:board-dsn).";
                LLENS_TOFU_PASSPHRASE_ITEM = "Keychain item of the tofu state passphrase (default llens:tofu:passphrase).";
              };
          # run by ship on the HOST (DATA visible) after the sandboxed flake check, before the push: the GQM
          # ratchet can only skip in the sandbox, so here a missing tree or a new gap refuses the push
          shipLocalChecks = [
            "LLENS_REQUIRE_GQM=1 nix develop -c pytest -q -p no:cacheprovider tests/test_grafana_gqm.py"
            # the site source's own tests live with it in DATA ([pipeline.plugins] of the site file): run them
            # here, on the host, against this checkout (the script: no bytecode or pytest cache lands in Drive)
            "nix develop -c bash tests/run-data-plugin-tests.sh"
          ];
        };

      # what stays OUTSIDE the tofu stack (perms, ds-password, bootstrap): secrets only via `secret exec`
      # (Keychain item NAMES, overridable), the stack URL from LLENS_GRAFANA_URL. Both apps run `llens grafana`:
      # running an app IS the operator's approval, so it passes --approve-network.
      grafanaAdmin =
        pkgs:
        pkgs.writeShellApplication {
          name = "llens-grafana-admin";
          text = ''
            : "''${LLENS_GRAFANA_URL:?set LLENS_GRAFANA_URL to the Grafana stack URL}"
            # `bootstrap` (one-time) runs with an org-admin token and mints the boards SA's own token
            tok="''${LLENS_GRAFANA_TOKEN_ITEM:-llens:grafana:token}"
            dsn="''${LLENS_PG_DSN_ITEM:-llens:db:board-dsn}"
            args=()
            if [ "''${1:-}" = bootstrap ]; then
              tok="''${LLENS_GRAFANA_ADMIN_TOKEN_ITEM:?name the org-admin token item}"
            fi
            args+=(GRAFANA_TOKEN="$tok")
            if secret ls | grep -xF "$dsn" >/dev/null; then args+=(LLENS_PG_DSN="$dsn"); fi
            export LLENS_REV="${self.shortRev or self.dirtyShortRev or "unknown"}"
            exec secret exec "''${args[@]}" -- ${lib.getExe' (llens pkgs) "llens"} grafana "$@" --approve-network
          '';
        };

      # the same objects as an OpenTofu stack (llens grafana tf, src/listing_lens/grafana/tf.py): provider
      # from nixpkgs, credentials + the state passphrase only via `secret exec`, state under
      # $LLENS_DATA/tofu (Drive), lock + run files under ~/.local/state.
      grafanaTf =
        pkgs:
        let
          tofu = pkgs.opentofu.withPlugins (p: [ p.grafana_grafana ]);
        in
        pkgs.writeShellApplication {
          name = "llens-grafana-tf";
          text = ''
            : "''${LLENS_GRAFANA_URL:?set LLENS_GRAFANA_URL to the Grafana stack URL}"
            : "''${LLENS_DATA:?set LLENS_DATA (the persona DATA root)}"
            export LLENS_TOFU=${lib.getExe tofu}
            exec secret exec \
              GRAFANA_TOKEN="''${LLENS_GRAFANA_TOKEN_ITEM:-llens:grafana:token}" \
              LLENS_PG_DSN="''${LLENS_PG_DSN_ITEM:-llens:db:board-dsn}" \
              LLENS_TOFU_PASSPHRASE="''${LLENS_TOFU_PASSPHRASE_ITEM:-llens:tofu:passphrase}" \
              -- ${lib.getExe' (llens pkgs) "llens"} grafana tf "$@" --approve-network
          '';
        };

      devPackagesFor = pkgs: [
        ((pythonFor pkgs).withPackages (
          ps:
          pyDepsFor pkgs ps
          ++ [
            ps.pytest
            ps.wcwidth
            (ps.mkPythonEditablePackage {
              pname = pyproject.project.name;
              inherit (pyproject.project) version scripts;
              root = "$PRJ_ROOT/src"; # expanded at runtime; set by the shellHook
              dependencies = pyDepsFor pkgs ps;
            })
          ]
        ))
        pkgs.ruff
        (pkgs.postgresql.withPackages (p: [ p.postgis ])) # export-db's local test server (initdb, pg_ctl, PostGIS)
        pkgs.check-jsonschema
        pkgs.gitleaks
        pkgs.git
        (harness.sshKeyTools pkgs) # ssh-keygen for approvals; NOT ssh (Nix's breaks git over SSH on macOS)
        pkgs.jq
      ];
      requiredBins = [
        "python3"
        consumer.cli
        "pytest"
        "ruff"
        "check-jsonschema"
        "gitleaks"
      ];

      treefmtEval = forAll (pkgs: treefmt-nix.lib.evalModule pkgs ./treefmt.nix);
    in
    {
      formatter = forAll (pkgs: treefmtEval.${pkgs.stdenv.hostPlatform.system}.config.build.wrapper);

      packages = forAll (
        pkgs:
        {
          default = llens pkgs;
          inherit (tools pkgs)
            env-doctor
            leak-gate
            public-gate
            ship
            ;
          grafana-admin = grafanaAdmin pkgs;
          grafana-tf = grafanaTf pkgs;
        }
        // (tools pkgs).appPackages # llens, preflight, cli-spec
      );

      apps = forAll (
        pkgs:
        lib.genAttrs [ "env-doctor" "leak-gate" "public-gate" "ship" "grafana-admin" "grafana-tf" ] (n: {
          type = "app";
          program = lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.${n};
          meta.description = "listing-lens ${n}";
        })
        // {
          default = {
            type = "app";
            program = lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.default;
            meta.description = "the llens CLI";
          };
        }
        # llens, preflight, cli-spec: the CLI with the checkout's .env* and .envrc.local loaded (direnv-allowed only)
        // (tools pkgs).apps
      );

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = devPackagesFor pkgs;
          PLAYWRIGHT_BROWSERS_PATH = harness.browsers pkgs;
          SCRAPE_HARNESS_FILTER_LISTS = harness.filterLists pkgs; # tracker blocking's pinned EasyList + EasyPrivacy
          PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
          # the site's source loads from DATA (Drive): never write its bytecode there
          PYTHONDONTWRITEBYTECODE = "1";
          # the harness plugin (hooks, agents, skills) at the locked rev: the tests run its guard
          SCRAPE_HARNESS_PLUGIN = "${scrape-harness.packages.${pkgs.stdenv.hostPlatform.system}.plugin}";
          shellHook = ''
            export PRJ_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
            [ -n "''${${consumer.data_env}:-}" ] || echo "${consumer.cli}: ${consumer.data_env} is unset (llens refuses to run)" >&2
          '';
        };
      });

      checks = forAll (
        pkgs:
        let
          system = pkgs.stdenv.hostPlatform.system;
        in
        self.packages.${system}
        // (tools pkgs).checks # claude-md-drift
        // {
          formatting = treefmtEval.${system}.config.build.check self;

          # GOES RED WHEN: the BUILT `llens` cannot start against THIS repo's pipeline.toml, for `llens --help`
          # or any declared command's `--help` (2026-11-08: `llens ab --help` failed under a dev shell whose
          # PYTHONPATH carried an older harness; the tests never ran the packaged CLI on the real file).
          # site_overlay: the CLI loads the site file before it parses an argument, and the smoke's DATA is
          # empty, so it runs on the synthetic site (as the tests do); without it the CLI refuses, by design.
          cli-smoke =
            (harness.cliSmoke {
              inherit pkgs consumer;
              package = self.packages.${system}.default;
              commands =
                let
                  toml = lib.importTOML ./pipeline.toml;
                in
                lib.unique (map (c: builtins.head (lib.splitString " " c.name)) (toml.stages ++ toml.utilities));
            }).overrideAttrs
              { LLENS_SITE_CONFIG = "${./tests/site}/site.toml"; };

          # GOES RED WHEN: ruff finds a lint error anywhere in the Python sources or tests.
          ruff = pkgs.runCommandLocal "ruff" { nativeBuildInputs = [ pkgs.ruff ]; } ''
            cd ${src} && ruff check --no-cache . && touch "$out"
          '';

          # GOES RED WHEN: one of THIS repo's record schemas is not valid 2020-12, an example stops
          # validating, or an INVALID example starts validating. (The harness's own run/event/approval/...
          # schemas are gated in the harness.)
          schemas =
            pkgs.runCommandLocal "schemas"
              {
                nativeBuildInputs = [ pkgs.check-jsonschema ];
              }
              ''
                # the source's sub-schemas (listing.v1 site, observation.v1 card) come by $ref from the source's
                # schema dir: here the tests' synthetic source's, as the real site's come from DATA
                cp -r ${./schemas} schemas && chmod -R u+w schemas && cd schemas
                cp ${./tests/site/plugins/sample_source/schemas}/*.schema.json .
                check-jsonschema --check-metaschema ./*.schema.json agents/*.schema.json
                for s in listing observation geocode advertiser panel; do
                  check-jsonschema --schemafile "$s.v1.schema.json" "examples/$s.example.json"
                done
                for bad in examples/invalid/*.json; do
                  s="''${bad##*/}"; s="''${s%%.*}"
                  if check-jsonschema --schemafile "$s.v1.schema.json" "$bad" >/dev/null 2>&1; then
                    echo "INVALID example passed: $bad" >&2; exit 1
                  fi
                done
                touch "$out"
              '';

          # GOES RED WHEN: a binary the dev shell promises does not resolve from devPackagesFor.
          toolchain-complete =
            pkgs.runCommandLocal "toolchain-complete" { nativeBuildInputs = devPackagesFor pkgs; }
              ''
                for b in ${lib.concatStringsSep " " requiredBins}; do
                  command -v "$b" >/dev/null || { echo "missing: $b" >&2; exit 1; }
                done
                touch "$out"
              '';
        }
      );
    };
}
