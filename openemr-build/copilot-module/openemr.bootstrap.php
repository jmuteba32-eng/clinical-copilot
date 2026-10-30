<?php
/**
 * Clinical Co-Pilot launcher + slide-out panel for the OpenEMR patient dashboard.
 * Panel holds an iframe scoped to the patient (?patient_id=<uuid>), resolved
 * from patient_data at render time. No panel header -- the co-pilot app has its
 * own -- just a floating close button so the two headers don't collide.
 */

use OpenEMR\Events\PatientDemographics\RenderEvent;

$eventDispatcher->addListener(
    RenderEvent::EVENT_RENDER_POST_PAGELOAD,
    function (RenderEvent $event) {
        $pid = $event->getPid();
        if (empty($pid)) {
            return;
        }

        $row = sqlQuery(
            "SELECT LOWER(CONCAT(
                SUBSTR(HEX(uuid),1,8),'-',SUBSTR(HEX(uuid),9,4),'-',
                SUBSTR(HEX(uuid),13,4),'-',SUBSTR(HEX(uuid),17,4),'-',
                SUBSTR(HEX(uuid),21,12))) AS uuid_formatted
             FROM patient_data WHERE pid = ?",
            [$pid]
        );
        $uuid = $row["uuid_formatted"] ?? null;
        if (empty($uuid)) {
            return;
        }

        $src = "https://agent-production-b516.up.railway.app/?patient_id=" . urlencode($uuid);
        ?>
        <style>
          #copilot-launcher {
            position: fixed; bottom: 24px; right: 24px; z-index: 99998;
            padding: 12px 18px; border-radius: 24px; border: none;
            background: #0b5ed7; color: #fff; font-weight: 600;
            font-family: sans-serif; cursor: pointer;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
          }
          #copilot-panel {
            position: fixed; top: 0; right: 0; height: 100vh; width: 560px;
            max-width: 100vw; z-index: 99999; background: #fff;
            box-shadow: -4px 0 16px rgba(0,0,0,0.25);
            transform: translateX(100%); transition: transform 0.25s ease;
          }
          #copilot-panel.open { transform: translateX(0); }
          #copilot-frame { border: none; width: 100%; height: 100%; display: block; }
          #copilot-panel-close {
            position: absolute; top: 8px; right: 10px; z-index: 100000;
            width: 28px; height: 28px; border-radius: 50%; border: none;
            background: rgba(0,0,0,0.45); color: #fff; font-size: 18px;
            line-height: 1; cursor: pointer;
          }
        </style>

        <button id="copilot-launcher" type="button"
                onclick="document.getElementById('copilot-panel').classList.toggle('open')">
          Clinical Co-Pilot
        </button>

        <div id="copilot-panel">
          <button id="copilot-panel-close" type="button" title="Close"
                  onclick="document.getElementById('copilot-panel').classList.remove('open')">&times;</button>
          <iframe id="copilot-frame" src="<?php echo attr($src); ?>"
                  allow="clipboard-write"></iframe>
        </div>
        <?php
    }
);

