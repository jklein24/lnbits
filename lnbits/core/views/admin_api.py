import os
import time
from http import HTTPStatus
from shutil import make_archive
from subprocess import Popen
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException

from lnbits.core.models import User
from lnbits.core.services import (
    get_balance_delta,
    update_cached_settings,
)
from lnbits.core.tasks import api_invoice_listeners
from lnbits.decorators import check_admin, check_super_user
from lnbits.server import server_restart
from lnbits.settings import AdminSettings, UpdateSettings, settings
from lnbits.tasks import invoice_listeners

from .. import core_app_extra
from ..crud import delete_admin_settings, get_admin_settings, update_admin_settings

admin_router = APIRouter(tags=["Admin UI"], prefix="/admin")


@admin_router.get(
    "/api/v1/audit",
    name="Audit",
    description="show the current balance of the node and the LNbits database",
    dependencies=[Depends(check_admin)],
)
async def api_auditor():
    try:
        delta, node_balance, total_balance = await get_balance_delta()
        return {
            "delta_msats": int(delta),
            "node_balance_msats": int(node_balance),
            "lnbits_balance_msats": int(total_balance),
        }
    except Exception as exc:
        raise HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            detail="Could not audit balance.",
        ) from exc


@admin_router.get(
    "/api/v1/monitor",
    name="Monitor",
    description="show the current listeners and other monitoring data",
    dependencies=[Depends(check_admin)],
)
async def api_monitor():
    return {
        "invoice_listeners": list(invoice_listeners.keys()),
        "api_invoice_listeners": list(api_invoice_listeners.keys()),
    }


@admin_router.get("/api/v1/settings", response_model=Optional[AdminSettings])
async def api_get_settings(
    user: User = Depends(check_admin),
) -> Optional[AdminSettings]:
    admin_settings = await get_admin_settings(user.super_user)
    return admin_settings


@admin_router.put(
    "/api/v1/settings",
    status_code=HTTPStatus.OK,
)
async def api_update_settings(data: UpdateSettings, user: User = Depends(check_admin)):
    await update_admin_settings(data)
    admin_settings = await get_admin_settings(user.super_user)
    assert admin_settings, "Updated admin settings not found."
    update_cached_settings(admin_settings.dict())
    core_app_extra.register_new_ratelimiter()
    return {"status": "Success"}


@admin_router.delete(
    "/api/v1/settings",
    status_code=HTTPStatus.OK,
    dependencies=[Depends(check_super_user)],
)
async def api_delete_settings() -> None:
    await delete_admin_settings()
    server_restart.set()


@admin_router.get(
    "/api/v1/restart",
    status_code=HTTPStatus.OK,
    dependencies=[Depends(check_super_user)],
)
async def api_restart_server() -> dict[str, str]:
    server_restart.set()
    return {"status": "Success"}


@admin_router.get(
    "/api/v1/backup",
    status_code=HTTPStatus.OK,
    dependencies=[Depends(check_super_user)],
    response_class=FileResponse,
)
async def api_download_backup() -> FileResponse:
    last_filename = "lnbits-backup"
    filename = f"lnbits-backup-{int(time.time())}.zip"
    db_url = settings.lnbits_database_url
    pg_backup_filename = f"{settings.lnbits_data_folder}/lnbits-database.dmp"
    is_pg = db_url and db_url.startswith("postgres://")

    if is_pg:
        p = urlparse(db_url)
        command = (
            f"pg_dump --host={p.hostname} "
            f"--dbname={p.path.replace('/', '')} "
            f"--username={p.username} "
            "--no-password "
            "--format=c "
            f"--file={pg_backup_filename}"
        )
        proc = Popen(
            command, shell=True, env={**os.environ, "PGPASSWORD": p.password or ""}
        )
        proc.wait()

    make_archive(last_filename, "zip", settings.lnbits_data_folder)

    # cleanup pg_dump file
    if is_pg:
        proc = Popen(f"rm {pg_backup_filename}", shell=True)
        proc.wait()

    return FileResponse(
        path=f"{last_filename}.zip", filename=filename, media_type="application/zip"
    )
