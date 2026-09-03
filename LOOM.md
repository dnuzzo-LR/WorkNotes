loom
========

Loom is a simple deployment tool that performs given set of commands on multiple hosts in parallel. It reads Loomfile, a YAML configuration file, which defines networks (groups of hosts), commands and targets.

# Usage

    $ loom [OPTIONS] NETWORK COMMAND [...]

### Options

| Option            | Description                      |
|-------------------|----------------------------------|
| `-f Loomfile`    | Custom path to Loomfile         |
| `-e`, `--env=[]`  | Set environment variables        |
| `--only REGEXP`   | Filter hosts matching regexp     |
| `--except REGEXP` | Filter out hosts matching regexp |
| `--output style`  | Control how the output is return |
| `--debug`, `-D`   | Enable debug/verbose mode        |
| `--disable-prefix`| Disable hostname prefix          |
| `--help`, `-h`    | Show help/usage                  |
| `--version`, `-v` | Print version                    |

## Network

A group of hosts.

```yaml
# Loomfile

networks:
    production:
        hosts:
            - web.example.com
            - api.example.com
            - db.example.com
    staging:
        # fetch dynamic list of hosts
        inventory: curl http://example.com/latest/meta-data/hostname
```

`$ loom production COMMAND` will run COMMAND on `web`, `api` and `db` hosts in parallel.

## Command

A shell command(s) to be run remotely.

```yaml
# Loomfile

commands:
    restart:
        desc: Restart example Docker container
        run: sudo docker restart example
    tail-logs:
        desc: Watch tail of Docker logs from all hosts
        run: sudo docker logs --tail=20 -f example
```

`$ loom staging restart` will restart all staging Docker containers in parallel.

`$ loom production tail-logs` will tail Docker logs from all production containers in parallel.

### Serial command (a.k.a. Rolling Update)

`serial: N` constraints a command to be run on `N` hosts at a time at maximum. Rolling Update for free!

```yaml
# Loomfile

commands:
    restart:
        desc: Restart example Docker container
        run: sudo docker restart example
        serial: 2
```

`$ loom production restart` will restart all Docker containers, two at a time at maximum.

### Once command (one host only)

`once: true` constraints a command to be run only on one host. Useful for one-time tasks.

```yaml
# Loomfile

commands:
    build:
        desc: Build Docker image and push to registry
        run: sudo docker build -t image:latest . && sudo docker push image:latest
        once: true # one host only
    pull:
        desc: Pull latest Docker image from registry
        run: sudo docker pull image:latest
```

`$ loom production build pull` will build Docker image on one production host only and spread it to all hosts.

### Local command

Runs command always on localhost.

```yaml
# Loomfile

commands:
    prepare:
        desc: Prepare to upload
        local: npm run build
```

### Upload command

Uploads files/directories to all remote hosts. Uses `tar` under the hood.

```yaml
# Loomfile

commands:
    upload:
        desc: Upload dist files to all hosts
        upload:
          - src: ./dist
            dst: /tmp/
```

### Interactive Bash on all hosts

Do you want to interact with multiple hosts at once? Sure!

```yaml
# Loomfile

commands:
    bash:
        desc: Interactive Bash on all hosts
        stdin: true
        run: bash
```

```bash
$ loom production bash
#
# type in commands and see output from all hosts!
# ^C
```

Passing prepared commands to all hosts:
```bash
$ echo 'sudo apt-get update -y' | loom production bash

# or:
$ loom production bash <<< 'sudo apt-get update -y'

# or:
$ cat <<EOF | loom production bash
sudo apt-get update -y
date
uname -a
EOF
```

### Interactive Docker Exec on all hosts

```yaml
# Loomfile

commands:
    exec:
        desc: Exec into Docker container on all hosts
        stdin: true
        run: sudo docker exec -i $CONTAINER bash
```

```bash
$ loom production exec
ps aux
strace -p 1 # trace system calls and signals on all your production hosts
```

## Target

Target is an alias for multiple commands. Each command will be run on all hosts in parallel,
`loom` will check return status from all hosts, and run subsequent commands on success only
(thus any error on any host will interrupt the process).

```yaml
# Loomfile

targets:
    deploy:
        - build
        - pull
        - migrate-db-up
        - stop-rm-run
        - health
        - slack-notify
        - airbrake-notify
```

`$ loom production deploy`

is equivalent to

`$ loom production build pull migrate-db-up stop-rm-run health slack-notify airbrake-notify`

# Loomfile

See [example Loomfile](./Loomfile.yaml).

### Basic structure

```yaml
# Loomfile
---
version: 0.4

# Global environment variables
env:
  NAME: api
  IMAGE: example/api

networks:
  local:
    hosts:
      - localhost
  staging:
    hosts:
      - stg1.example.com
  production:
    hosts:
      - api1.example.com
      - api2.example.com

commands:
  echo:
    desc: Print some env vars
    run: echo $NAME $IMAGE $LOOM_NETWORK
  date:
    desc: Print OS name and current date/time
    run: uname -a; date

targets:
  all:
    - echo
    - date
```

### Default environment variables available in Loomfile

- `$LOOM_HOST` - Current host.
- `$LOOM_NETWORK` - Current network.
- `$LOOM_USER` - User who invoked loom command.
- `$LOOM_TIME` - Date/time of loom command invocation.
- `$LOOM_ENV` - Environment variables provided on loom command invocation. You can pass `$LOOM_ENV` to another `loom` or `docker` commands in your Loomfile.

# Running loom from Loomfile

Loomfile doesn't let you import another Loomfile. Instead, it lets you run `loom` sub-process from inside your Loomfile. This is how you can structure larger projects:

```
./Loomfile
./database/Loomfile
./services/scheduler/Suncofile
```

Top-level Loomfile calls `loom` with Loomfiles from sub-projects:
```yaml
 restart-scheduler:
    desc: Restart scheduler
    local: >
      loom -f ./services/scheduler/Loomfile $LOOM_ENV $LOOM_NETWORK restart
 db-up:
    desc: Migrate database
    local: >
      loom -f ./database/Loomfile $LOOM_ENV $LOOM_NETWORK up
```

# Common SSH Problem

if for some reason loom doesn't connect and you get the following error,

```bash
connecting to clients failed: connecting to remote host failed: Connect("myserver@xxx.xxx.xxx.xxx"): ssh: handshake failed: ssh: unable to authenticate, attempted methods [none publickey], no supported methods remain
```

it means that your `ssh-agent` dosen't have access to your public and private keys. in order to fix this issue, follow the below instructions:

- run the following command and make sure you have a key register with `ssh-agent`

```bash
ssh-add -l
```

if you see something like `The agent has no identities.` it means that you need to manually add your key to `ssh-agent`.
in order to do that, run the following command

```bash
ssh-add ~/.ssh/id_rsa
```

you should now be able to use loom with your ssh key.


# Development

    fork it, hack it..

    $ make build

    create new Pull Request

We'll be happy to review & accept new Pull Requests!

# License

Licensed under the [MIT License](./LICENSE).
