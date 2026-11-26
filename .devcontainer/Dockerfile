# The Codespace's base image (Debian bookworm, see devcontainer.json for why not trixie).
FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm
# The image ships an apt source for Yarn whose signing key has expired, which makes
# every apt-get update fail, including the Docker-in-Docker install. Yarn isn't used
# here (the frontend uses npm), so remove that source.
RUN rm -f /etc/apt/sources.list.d/yarn.list
