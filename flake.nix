{
  description = "Dev shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems
        (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        amux = pkgs.python313Packages.buildPythonApplication {
          pname = "amux";
          version = "0.1.0";
          pyproject = true;
          src = ./.;

          build-system = [ pkgs.python313Packages.setuptools ];
          dependencies = with pkgs.python313Packages; [
            mcp
            libtmux
          ];

          makeWrapperArgs = [ "--prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.tmux ]}" ];

          meta.mainProgram = "amux";
        };
        default = amux;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            python313
            tmux
          ];
        };
      });
    };
}
