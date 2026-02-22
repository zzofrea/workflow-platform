; Weekly health check finds no issues.
GIVEN the system has been running normally for a week.
WHEN the weekly health check runs.
THEN a single notification appears with severity "success" containing disk usage percentage, memory usage percentage, and container count.

; Weekly health check catches disk pressure.
GIVEN one mount point exceeds 85% disk usage.
WHEN the weekly health check runs.
THEN a notification appears with severity "warning" identifying the mount and its usage percentage.

; Weekly health check catches memory pressure.
GIVEN memory usage exceeds 90%.
WHEN the weekly health check runs.
THEN a notification appears with severity "warning" reporting the memory usage percentage.

; Weekly health check catches missing containers.
GIVEN one or more expected containers are not running.
WHEN the weekly health check runs.
THEN a notification appears with severity "warning" listing the missing container names.

; Multiple threshold breaches produce a single notification.
GIVEN disk is over 85% AND memory is over 90% AND a container is down.
WHEN the weekly health check runs.
THEN a single notification fires with severity "warning" listing all three findings in one message.

; Health check fails when Docker is unreachable.
GIVEN the Docker daemon is not responding.
WHEN the weekly health check runs.
THEN a notification fires with severity "critical" stating the Docker daemon is unreachable.

; Clean boot with all containers up.
GIVEN the system has just rebooted and all expected containers start successfully.
WHEN 5 minutes have elapsed since boot.
THEN a notification appears listing all containers as running with severity "success".

; Boot with missing expected containers.
GIVEN the system has rebooted but some expected containers failed to start.
WHEN 5 minutes have elapsed since boot.
THEN a notification appears with severity "warning" listing the missing expected containers by name and showing the full container inventory.
