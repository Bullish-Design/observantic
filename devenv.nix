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

  # devman — the automation plane (CONCEPT.md §5). Three lines: what to join,
  # who this repository is, and which workflow groups it inherits.
  #
  # `project` is identity and never a path. It is stated rather than derived
  # from the directory name, so renaming the checkout does not re-register this
  # repository as a new one and lose its run history.
  devman = {
    enable = true;
    project = "observantic";
    groups = [ "base" "release" ];
  };

  # https://devenv.sh/tasks/
  #
  # The three task names the `python` group's workflows call. The group states
  # WHICH tasks run and in what order; this repository states what each one IS.
  # Nothing in the group file mentions ruff, mypy or pytest — that is what lets
  # one unedited group file serve every repository that takes the group.
  #
  # The `python:` namespace is devenv's requirement, not the plane's: a bare
  # `lint` is an evaluation error. The namespace is the group's own name, so two
  # groups' `lint` cannot collide in a repository that takes both.
  tasks = {
    "python:lint".exec = "uv run ruff check .";
    "python:typecheck".exec = "uv run mypy";
    "python:test".exec = "uv run pytest";

    # What the `release` group's one workflow builds here (stage 4). The group
    # names the task and this repository names the tool, so `release.yaml` needs
    # no edit and holds no absolute path.
    #
    # `.devman/.runs/artifacts/` is where a run's output goes (CONCEPT.md §9.2).
    # It is created at registration and git-ignored, so a built wheel never
    # dirties the tree, and the release workflow lists what appeared there.
    "release:build".exec = "uv build --out-dir .devman/.runs/artifacts";

    # `base`'s two names, aliased onto the tasks above (stage 5). A devenv task
    # with only `after` and no `exec` runs its dependency and fails when that
    # dependency fails, so this duplicates no command body.
    #
    # WHY THIS REPOSITORY TAKES `base` AS WELL. `review` and `maintain` live in
    # `base` and are not ecosystem content: one runs git and the group's two
    # names, the other prunes `.devman/.runs/` and asks `devman doctor`. Copying
    # them into `python` would put one file in two groups and give this
    # repository a second copy to keep in step (CONCEPT.md §3.1). `python`
    # shadows `check` and `validate` as before, and `base` adds what only it has
    # (STAGE_5_LOG.md, S5).
    "base:check".after = [ "python:lint" "python:typecheck" ];
    "base:test".after = [ "python:test" ];
  };

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
