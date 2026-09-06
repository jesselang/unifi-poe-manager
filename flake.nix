{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.flake-utils.url = "github:numtide/flake-utils";

  outputs = { self, nixpkgs, flake-utils }:
    let
      pythonPackages = ps: with ps; [
        fastapi
        uvicorn
        apscheduler
        sqlalchemy
        aiohttp
        jinja2
        aiounifi
      ];
    in
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;
        pythonEnv = python.withPackages pythonPackages;
        devPythonEnv = python.withPackages (ps: pythonPackages ps ++ [ ps.pytest ]);
      in {
        devShells.default = pkgs.mkShell {
          packages = [ devPythonEnv pkgs.sqlite ];
        };

        packages.default = pkgs.stdenv.mkDerivation {
          pname = "ap-controller";
          version = "0.1.0";
          src = ./.;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          dontBuild = true;
          installPhase = ''
            mkdir -p $out/share/ap-controller $out/bin
            cp minimal.py $out/share/ap-controller/
            makeWrapper ${pythonEnv}/bin/python3 $out/bin/ap-controller \
              --add-flags $out/share/ap-controller/minimal.py
          '';
        };
      }) // {
        nixosModules.default = { config, lib, pkgs, ... }:
          with lib;
          let
            cfg = config.services.ap-controller;
          in {
            options.services.ap-controller = {
              enable = mkEnableOption "AP schedule controller (PoE on/off via UniFi API)";

              configFile = mkOption {
                type = types.path;
                default = "/etc/ap-controller/config.toml";
                description = ''
                  Path to config.toml on the target machine, outside the Nix
                  store. Must contain the controller password, so it should
                  be mode 600 and owned by the ap-controller user. This file
                  is not managed by Nix — create/edit it by hand.
                '';
              };
            };

            config = mkIf cfg.enable {
              users.users.ap-controller = {
                isSystemUser = true;
                group = "ap-controller";
              };
              users.groups.ap-controller = { };

              systemd.services.ap-controller = {
                description = "AP Schedule Controller";
                after = [ "network-online.target" ];
                wants = [ "network-online.target" ];
                wantedBy = [ "multi-user.target" ];
                environment.AP_CONTROLLER_CONFIG = cfg.configFile;
                serviceConfig = {
                  User = "ap-controller";
                  Group = "ap-controller";
                  PrivateTmp = true;
                  ExecStart = "${self.packages.${pkgs.system}.default}/bin/ap-controller";
                  Restart = "always";
                  RestartSec = "5s";
                };
              };
            };
          };
      };
}
