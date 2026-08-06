{ pkgs, lib, config, inputs, ... }:

{
  # https://devenv.sh/basics/
  dotenv.enable = true;

  env.GREET = "devenv";

  # https://devenv.sh/packages/
  packages = with pkgs; [
    # Add your packages here:
    git
    
  ];

  

  # https://devenv.sh/languages/
  # languages.rust.enable = true;
  languages.python = {
    enable = true;
    version = "3.13";
    venv.enable = true;
    uv.enable = true;

  };

  # https://devenv.sh/services/
  services.postgres = {
    enable = true;
    package = pkgs.postgresql_17;
    initialScript = ''CREATE USER postgres WITH PASSWORD 'postgres'; ALTER USER postgres WITH SUPERUSER;'';
    initialDatabases = [
      { 
        name = "eventic"; 
        user= "postgres"; 
        pass = "postgres"; 
      }
    ]; 
    listen_addresses = "127.0.0.1";
    port = 5432;
    settings = { 
      unix_socket_directories = "/tmp";
      };
    };

  # https://devenv.sh/scripts/
  scripts.hello.exec = ''
    echo
    echo hello from $GREET
    echo
  '';

  enterShell = ''
    echo
    hello
    echo
    git --version
    echo
  '';

  # https://devenv.sh/tasks/
  # tasks = {
  #   "myproj:setup".exec = "mytool build";
  #   "devenv:enterShell".after = [ "myproj:setup" ];
  # };

  # https://devenv.sh/tests/
  # Tests run on SQLite by default (eventic 1.1.0 backend). The devenv
  # Postgres at 127.0.0.1:5432 (db "eventic", user/pass postgres/postgres)
  # is available for optional Postgres integration tests:
  #   TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/eventic \
  #     uv run pytest tests/test_postgres_integration.py
  enterTest = ''
    echo "Running tests"
    uv run pytest
  '';

  # https://devenv.sh/git-hooks/
  # git-hooks.hooks.shellcheck.enable = true;

  # See full reference at https://devenv.sh/reference/options/
}
