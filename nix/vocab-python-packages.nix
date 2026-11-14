# The service-vocabulary stage's Python packages that nixpkgs lacks (plan v2, operator-approved
# 2026-11-08). Both MIT (checked in each sdist's LICENSE), pinned by hash; built from source.
{ pkgs, ps }:
{
  # negspaCy: NegEx negation for spaCy, with a custom termset (pure Python; needs spacy >=3.8,<4).
  negspacy = ps.buildPythonPackage rec {
    pname = "negspacy";
    version = "1.1.0";
    pyproject = true;
    src = ps.fetchPypi {
      inherit pname version;
      hash = "sha256-E9jE170bn1x+g7wdfkaJynCs4yMriww4wio4B/wLmh8=";
    };
    build-system = [ ps.hatchling ];
    dependencies = [ ps.spacy ];
    pythonImportsCheck = [ "negspacy" ];
  };

  # Schwartz–Hearst abbreviation–definition pairs (Rust core, pyo3; built with maturin, crates vendored).
  abbreviation-extractor = ps.buildPythonPackage rec {
    pname = "abbreviation_extractor";
    version = "0.2.0";
    pyproject = true;
    src = ps.fetchPypi {
      inherit pname version;
      hash = "sha256-M9FB7pf/MU0jtyhDvO7m7MpmO7Uocf6bMAR8YRX2xfc=";
    };
    cargoDeps = pkgs.rustPlatform.fetchCargoVendor {
      inherit src;
      name = "${pname}-${version}";
      hash = "sha256-dge4bqtz0uVJf9TF2FuCdokGA/+8h8JDe0t894FH8Rg=";
    };
    nativeBuildInputs = with pkgs.rustPlatform; [
      cargoSetupHook
      maturinBuildHook
    ];
    pythonImportsCheck = [ "abbreviation_extractor" ];
  };
}
