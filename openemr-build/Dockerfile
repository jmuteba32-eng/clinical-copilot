# OpenEMR, plus this fork's Co-Pilot menu tab.
#
# Deployed with `railway up --service openemr` from this directory, the same way
# agent/ is deployed -- Railway cannot connect to this project's self-hosted
# GitLab, so both services upload a build context rather than tracking a repo.
#
# This layers exactly ONE file onto the stock image. Deliberately not a rebuild
# from source: the published image is known-good and does its own first-boot
# database setup, and putting that at risk to change one JSON file is a bad
# trade. Layering keeps every guarantee the image makes and overwrites one thing.
FROM openemr/openemr:7.0.3

COPY standard.json \
     /var/www/localhost/htdocs/openemr/interface/main/tabs/menu/menus/standard.json
