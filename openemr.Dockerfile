# OpenEMR, plus this fork's menu addition.
#
# Railway currently deploys the stock openemr/openemr:7.0.3 image, which means
# nothing in this repo's interface/ tree reaches the running container. This
# layers exactly one file on top of the stock image: the tab menu, with a
# Clinical Co-Pilot entry added.
#
# Deliberately NOT a rebuild from source. The stock image is known-good and does
# its own first-boot database setup; rebuilding OpenEMR from this fork would put
# that at risk to change one JSON file. Layering keeps every guarantee the
# published image makes and overwrites one thing.
FROM openemr/openemr:7.0.3

COPY interface/main/tabs/menu/menus/standard.json \
     /var/www/localhost/htdocs/openemr/interface/main/tabs/menu/menus/standard.json
