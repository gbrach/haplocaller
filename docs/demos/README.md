# Terminal demos

One script per cluster. Record on the cluster itself, in a fresh clone, so the install is real:

```bash
pip install --user asciinema          # or: brew install asciinema (on a laptop, then ssh inside)
asciinema rec -c 'bash demo-unistra.sh' unistra.cast
```

Each script pauses between steps and types nothing for you: it prints the step, then you run it.
The small test submits one GPU job of at most 1,000 reads. Do not record a full run.
