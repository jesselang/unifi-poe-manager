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
        python-dotenv
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

            portSubmodule = types.submodule {
              options = {
                deviceMac = mkOption { type = types.str; };
                portIdx = mkOption { type = types.int; };
                onMode = mkOption {
                  type = types.enum [ "auto" "pasv24" "passthrough" ];
                  default = "auto";
                };
                offHour = mkOption { type = types.nullOr (types.ints.between 0 23); default = null; };
                offMinute = mkOption { type = types.nullOr (types.ints.between 0 59); default = null; };
                onHour = mkOption { type = types.nullOr (types.ints.between 0 23); default = null; };
                onMinute = mkOption { type = types.nullOr (types.ints.between 0 59); default = null; };
              };
            };

            configFile = (pkgs.formats.toml { }).generate "ap-controller-config.toml" {
              controller = {
                host = cfg.controller.host;
                port = cfg.controller.port;
                site = cfg.controller.site;
              };
              schedule = {
                off_hour = cfg.schedule.offHour;
                off_minute = cfg.schedule.offMinute;
                on_hour = cfg.schedule.onHour;
                on_minute = cfg.schedule.onMinute;
                timezone = cfg.schedule.timezone;
              };
              ports = map (p: filterAttrs (n: v: v != null) {
                device_mac = p.deviceMac;
                port_idx = p.portIdx;
                on_mode = p.onMode;
                off_hour = p.offHour;
                off_minute = p.offMinute;
                on_hour = p.onHour;
                on_minute = p.onMinute;
              }) cfg.ports;
            };
          in {
            options.services.ap-controller = {
              enable = mkEnableOption "AP schedule controller (PoE on/off via UniFi API)";

              environmentFile = mkOption {
                type = types.path;
                example = "/run/secrets/ap-controller-env";
                description = ''
                  Path (outside the Nix store) to an EnvironmentFile holding
                  AP_CONTROLLER_USERNAME and AP_CONTROLLER_PASSWORD, e.g.:

                    AP_CONTROLLER_USERNAME=ap-scheduler
                    AP_CONTROLLER_PASSWORD=hunter2

                  This is the only place credentials live — everything else
                  is declared in Nix and safe to commit. Not managed by this
                  module; create it by hand (mode 600, owned by root is
                  fine — systemd reads it before dropping privileges) or
                  point it at a sops-nix/agenix secret.
                '';
              };

              controller = {
                host = mkOption { type = types.str; };
                port = mkOption { type = types.port; default = 8443; };
                site = mkOption { type = types.str; default = "default"; };
              };

              schedule = {
                offHour = mkOption { type = types.ints.between 0 23; default = 23; };
                offMinute = mkOption { type = types.ints.between 0 59; default = 59; };
                onHour = mkOption { type = types.ints.between 0 23; default = 6; };
                onMinute = mkOption { type = types.ints.between 0 59; default = 0; };
                timezone = mkOption { type = types.str; default = "UTC"; };
              };

              ports = mkOption {
                type = types.listOf portSubmodule;
                default = [ ];
                description = "Switch ports to schedule. Each may override the global schedule.";
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
                environment.AP_CONTROLLER_CONFIG = configFile;
                serviceConfig = {
                  User = "ap-controller";
                  Group = "ap-controller";
                  PrivateTmp = true;
                  EnvironmentFile = cfg.environmentFile;
                  ExecStart = "${self.packages.${pkgs.system}.default}/bin/ap-controller";
                  Restart = "always";
                  RestartSec = "5s";
                };
              };
            };
          };
      };
}
