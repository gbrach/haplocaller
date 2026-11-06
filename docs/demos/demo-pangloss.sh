#!/usr/bin/env bash
# Demo for pangloss: install, a small basecalling test, watch, results.  Run inside asciinema.
step() { printf '\n\033[36m### %s\033[0m\n' "$1"; read -r -p "Press Enter to run: $2 " _; eval "$2"; }
step "Install"                 "git clone https://github.com/gbrach/haplocaller.git && cd haplocaller && ./install.sh"
step "Start the wizard (choose: small test)" "haplocaller"
step "Follow the job"          "haplocaller watch"
step "Go to the results"       "cd /ssd_workspace/$USER && ls && ls */fastq | head"
