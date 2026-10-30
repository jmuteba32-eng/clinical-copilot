FROM openemr/openemr:7.0.3

# Pre-seeded DB config. Harmless now that the sites/ volume shadows this path,
# but kept so a volume-less rebuild still has valid connection settings.
COPY sqlconf.php /var/www/localhost/htdocs/openemr/sites/default/sqlconf.php
RUN chown apache:apache /var/www/localhost/htdocs/openemr/sites/default/sqlconf.php \
 && chmod 444 /var/www/localhost/htdocs/openemr/sites/default/sqlconf.php

# Restore the stock menu at build time. A prior custom standard.json was a
# truncated stub that killed main.php with JSON ERROR: 4.
RUN curl -fsSL -o /var/www/localhost/htdocs/openemr/interface/main/tabs/menu/menus/standard.json \
    "https://raw.githubusercontent.com/openemr/openemr/v7_0_3/interface/main/tabs/menu/menus/standard.json"

# Clinical Co-Pilot launcher module. Must be baked into the image: the
# custom_modules path is NOT under sites/, so the Railway volume won't persist it.
COPY copilot-module /var/www/localhost/htdocs/openemr/interface/modules/custom_modules/oe-module-copilot
RUN chown -R apache:apache /var/www/localhost/htdocs/openemr/interface/modules/custom_modules/oe-module-copilot