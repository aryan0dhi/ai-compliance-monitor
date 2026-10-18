"""Model registry.

Import every module's models here so `Base.metadata` is complete for Alembic autogenerate.
When you add `app/modules/<name>/models.py`, add an import line below.
"""

from app.db.base import Base  # noqa: F401
from app.modules.auth import models as auth_models  # noqa: F401
from app.modules.events import models as events_models  # noqa: F401
from app.modules.ledger import models as ledger_models  # noqa: F401
from app.modules.metrics import models as metrics_models  # noqa: F401
from app.modules.notifications import models as notifications_models  # noqa: F401
from app.modules.systems import models as systems_models  # noqa: F401
from app.modules.testing import models as testing_models  # noqa: F401
from app.modules.thresholds import models as thresholds_models  # noqa: F401
from app.modules.worker import models as worker_models  # noqa: F401
