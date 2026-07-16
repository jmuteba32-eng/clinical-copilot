# openemr-build

Build context for the OpenEMR service.

## Why this exists

Railway deploys OpenEMR from the stock `openemr/openemr:7.0.3` image, so nothing
in this repo's `interface/` tree reaches the running container. This directory
layers one file onto that image: the tab menu, with a Clinical Co-Pilot entry.

Railway cannot connect to this project's self-hosted GitLab, so this is deployed
by uploading a build context, exactly like `agent/`:

```bash
cd openemr-build
railway up --service openemr
```

## Keeping standard.json in sync

`standard.json` here is a copy of `interface/main/tabs/menu/menus/standard.json`.
If you change the menu, change it there first and copy it here — the fork's tree
stays the source of truth even though the container is built from this copy.

## Revert

If OpenEMR breaks, set the service's Source Image back to `openemr/openemr:7.0.3`
in Railway and redeploy. Nothing else in the stack is affected.
