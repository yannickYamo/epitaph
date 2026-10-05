# Security

*epitaph* runs a language model on a small computer in a public space. The design keeps that
safe:

- **The model has no network.** Its process runs in its own cgroup, and an nftables rule matching
  that cgroup refuses its outbound traffic. It talks only to the controller over localhost.
- **Nothing from the network reaches the model.** The planned feed of last words, not built
  yet, only writes out.
- **Local only:** the event bus and control channel listen on 127.0.0.1. Remote viewing goes
  through an SSH tunnel.
- **Key-only SSH** over the network.
- **No secrets in the repository.** Wi-Fi credentials live only in the Pi's root-only
  NetworkManager store. Model weights are downloaded and verified against pinned sha256 values.

## Reporting a vulnerability

Please open a [private security advisory](https://github.com/yannickYamo/epitaph/security/advisories/new)
rather than a public issue. You will get an answer within a week.
