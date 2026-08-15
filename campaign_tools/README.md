# Trusted campaign tools

This directory contains the laboratory-aware revision of the current 0D
campaign generator plus example scientific/configuration files.

The generator is trusted infrastructure: the agent may invoke it and create new
campaign TOMLs, but should not modify the generator source during ordinary
research operation.

Machine paths are obtained from `config/laboratory.toml`.
