import time

from celery.decorators import task
from celery.result import AsyncResult

from chromogenic.tasks import machine_imaging_task, machine_migration_task
from chromogenic.drivers.openstack import ImageManager as OSImageManager
from chromogenic.drivers.eucalyptus import ImageManager as EucaImageManager

from threepio import logger

from atmosphere import settings

from core.email import send_image_request_email
from core.models.machine_request import MachineRequest, process_machine_request

from service.deploy import freeze_instance, sync_instance
from service.tasks.driver import deploy_to


def start_machine_imaging(machine_request, delay=False):
    """
    Builds up a machine imaging task using the core.models.machine_request object
    delay - If true, wait until task is completed before returning
    """
    #NOTE: Do not move up -- Circular dependency
    machine_request.status = 'processing'
    machine_request.save()
    instance_id = machine_request.instance.provider_alias

    (orig_managerCls, orig_creds,
     dest_managerCls, dest_creds) = machine_request.prepare_manager()
    imaging_args = machine_request.get_imaging_args()

    #Step 1 - On OpenStack, sync/freeze BEFORE starting migration/imaging
    if orig_managerCls == OSImageManager:
        freeze_task = freeze_instance_task.si(machine_request.id, instance_id)
        init_task = freeze_task
    if dest_managerCls and dest_creds != orig_creds:
        #Will run machine imaging task..
        migrate_task = machine_migration_task.si(
                orig_managerCls, orig_creds, dest_managerCls, dest_creds,
                **imaging_args)
        if not init_task:
            init_task = migrate_task
        else:
            init_task.link(migrate_task)
    else:
        image_task = machine_imaging_task.si(orig_managerCls, orig_creds,
                                             **imaging_args)
        if not init_task:
            init_task = image_task
        else:
            init_task.link(image_task)
    process_task = process_request.subtask((machine_request.id,))
    #After init_task is completed (And any other links..) process the request
    init_task.link(process_task)
    result = init_task.apply_async(link_error=machine_request_error.s(machine_request.id,))
    if delay:
        result.get()
    return result


def set_machine_request_metadata(machine_request, machine):
    (orig_managerCls, orig_creds,
        new_managerCls, new_creds) = machine_request.prepare_manager()
    if not new_manager:
        manager = orig_managerCls(**orig_creds)
    else:
        manager = new_managerCls(**new_creds)
    lc_driver = manager.admin_driver._connection
    if not hasattr(lc_driver, 'ex_set_image_metadata'):
        return
    lc_driver.ex_set_image_metadata(machine, {'deployed':'True'})
    if machine_request.new_machine_description:
        metadata['description'] = machine_request.new_machine_description
    if machine_request.new_machine_tags:
        metadata['tags'] = machine_request.new_machine_tags
    logger.info("LC Driver:%s - Machine:%s - Metadata:%s" % (lc_driver,
            machine.id, metadata))
    lc_driver.ex_set_image_metadata(machine, metadata)
    return machine



@task(name='machine_request_error', ignore_result=False)
def machine_request_error(result_uuid, machine_request_id):
    result = AsyncResult(uuid)
    exc = result.get(propagate=False)
    err_str = "Task %r raised exception: %r\n%r" % (exc, result.traceback)
    logger.error(err_str)
    machine_request = MachineRequest.objects.get(id=machine_request_id)
    machine_request.status = err_str
    machine_request.save()


@task(name='process_request', ignore_result=False)
def process_request(new_image_id, machine_request_id):
    machine_request = MachineRequest.objects.get(id=machine_request_id)
    set_machine_request_metadata(machine_request, machine)
    process_machine_request(machine_request, new_image_id)
    send_image_request_email(machine_request.new_machine_owner,
                             machine_request.new_machine,
                             machine_request.new_machine_name)

@task(name='freeze_instance_task', ignore_result=False)
def freeze_instance_task(machine_request_id, instance_id):
    from api import get_esh_driver
    machine_request = MachineRequest.objects.get(id=machine_request_id)
    identity = machine_request.instance.created_by_identity
    driver = get_esh_driver(identity)
    kwargs = {}
    private_key = "/opt/dev/atmosphere/extras/ssh/id_rsa"
    kwargs.update({'ssh_key': private_key})
    kwargs.update({'timeout': 120})

    si_script = sync_instance()
    kwargs.update({'deploy': si_script})

    instance = driver.get_instance(instance_id)
    driver.deploy_to(instance, **kwargs)

    fi_script = freeze_instance()
    kwargs.update({'deploy': fi_script})
    deploy_to.delay(
        driver.__class__, driver.provider, driver.identity,
        instance.id, **kwargs)
    #Give it a head-start..
    time.sleep(1)
    return
