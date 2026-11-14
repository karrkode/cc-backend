# CC-183: the Caddy image the API server runs, pinned by digest. Nothing builds this file:
# it's here so Dependabot's docker updates see the pin and propose bumps. The deploy job reads
# the FROM line and passes it to culture-crew-deploy, which recreates Caddy when it changes.
FROM public.ecr.aws/docker/library/caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
