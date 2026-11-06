#!/usr/bin/env bash
# Demo for unistra: install, a small basecalling test, watch, results.  Run inside asciinema.
step() { printf '\n\033[36m### %s\033[0m\n' "$1"; read -r -p "Press Enter to run: $2 " _; eval "$2"; }
step "Install"                 "git clone https://github.com/gbrach/haplocaller.git && cd haplocaller && ./install.sh --dorado download"
step "Start the wizard (choose: small test)" "haplocaller"
step "Follow the job"          "haplocaller watch"
step "Go to the results"       "cd ~/haplocaller_runs && ls && ls */fastq | head"
