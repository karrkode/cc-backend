# CC-183: the Caddy image the API server runs, pinned by digest. Nothing builds this file:
# it's here so Dependabot's docker updates see the pin and propose bumps. The deploy job reads
# the FROM line and passes it to culture-crew-deploy, which recreates Caddy when it changes.
FROM public.ecr.aws/docker/library/caddy:2.11.7-alpine@sha256:e70b164da81ef698b3ad19af799ea6086ce6d9e48ed4079eb9fe26f6fbf14ee5
