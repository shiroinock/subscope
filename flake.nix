{
  description = "開発系サブスク(Claude・Codex・GitHub)のクオータをどれだけ使えているかを測る";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachSystem [ "aarch64-darwin" "x86_64-linux" "aarch64-linux" ] (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python3;
        subscope = python.pkgs.buildPythonApplication {
          pname = "subscope";
          version = "0.1.0";
          pyproject = true;
          src = pkgs.lib.cleanSource ./.;
          build-system = [ python.pkgs.setuptools ];
          nativeBuildInputs = [ pkgs.makeWrapper ];
          nativeCheckInputs = [ python.pkgs.unittestCheckHook ];
          unittestFlags = [
            "-s"
            "tests"
          ];
          # actions と github の取り込みは gh を呼ぶ。
          makeWrapperArgs = [
            "--prefix"
            "PATH"
            ":"
            (pkgs.lib.makeBinPath [ pkgs.gh ])
          ];
          pythonImportsCheck = [ "subscope" ];
          meta = {
            description = "開発系サブスクのクオータの使い方を測る";
            license = pkgs.lib.licenses.mit;
            mainProgram = "subscope";
          };
        };
      in
      {
        packages.default = subscope;
        apps.default = {
          type = "app";
          program = pkgs.lib.getExe subscope;
          meta.description = "開発系サブスクのクオータの使い方を測る";
        };
        checks = {
          package = subscope;
          ruff = pkgs.runCommand "subscope-ruff" { nativeBuildInputs = [ pkgs.ruff ]; } ''
            cd ${./.}
            ruff check --no-cache src tests
            ruff format --no-cache --check src tests
            touch $out
          '';
        };
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.ruff
            pkgs.gh
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
        formatter = pkgs.nixfmt;
      }
    );
}
