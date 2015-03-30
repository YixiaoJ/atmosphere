#!/usr/bin/env python
import argparse
import time

from service.driver import get_esh_driver
from core.models import ProviderMachine, Provider, Identity, Application, MachineRequest
from core.models.application import _generate_app_uuid
from service.driver import get_admin_driver, get_account_driver
from service.instance import suspend_instance

#Ghetto cache ftw
Force = True
Images = []

def main():
    global Force, Images
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-list", action="store_true",
                        help="List of provider names and IDs")
    parser.add_argument("--provider-id", type=int,
                        help="Atmosphere provider ID"
                        " to use when importing users.")
    parser.add_argument("--identify", action="store_true",
                        help="Identify possible troublesome ProviderMachine/Application associations.")
    parser.add_argument("--force", action="store_true",
                        help="Force a change-action regardless of state of the image.")
    parser.add_argument("--fix_list",
                        help="List of identifiers you want to 'fix' (Comma-Seperated)")
    args = parser.parse_args()

    if args.provider_list:
        print "ID\tName"
        for p in Provider.objects.all().order_by('id'):
            print "%d\t%s" % (p.id, p.location)
        return
    if not args.provider_id:
        print "ERROR: provider-id is required. To get a list of providers use"\
            " --provider-list"
        return

    provider = Provider.objects.get(id=args.provider_id)
    print "Provider Selected:%s" % provider

    accounts = get_account_driver(provider)
    Images = accounts.image_manager.admin_list_images()
    (deleted_list, incorrect_list, create_list, name_match_list, correct_list) = _identify_problem_apps(provider, accounts)
    image_lists = (deleted_list, incorrect_list, create_list, name_match_list, correct_list)
    Force = True if args.force else False

    if args.identify:
        print "Deleted Images\n---"
        for image in deleted_list:
            print "%s" % image
        print "\n\n"
        print "Correct\n---"
        for image in correct_list:
            print "%s" % image
        print "\n\n"
        print "Names Match (Fix/Create if association is wrong!)\n---"
        for image in name_match_list:
            print "%s" % image
        print "\n\n"
        print "Create Images (Fix/Create if association is wrong!)\n---"
        for image in create_list:
            print "%s" % image
        print "\n\n"
        print "Incorrect Images (Fix if association is wrong!)\n---"
        for image in incorrect_list:
            print "%s" % image
        print "\n\n"

    if args.fix_list:
        images = args.fix_list.split(",")
        for image_id in images:
            if image_id in deleted_list:
                print "FIXED: %s was deleted on this provider. Skipping" % image_id
                continue
            elif image_id in correct_list:
                print "FIXED: %s is already pointing to the correct Application by UUID. Skipping" % image_id
                continue
            elif image_id in name_match_list:
                if not Force:
                    print "FIXED: %s is already pointing to the correct Application by Name. "\
                        "To create a new Application by UUID pass the '--force' option."
                    continue
                # Falls through
            elif image_id in create_list:
                print "Fixing -- Creating a new application (by UUID) for providermachine."
                # Falls through
            elif image_id in incorrect_list:
                print "Fixing -- Re-connecting providermachine to the application (with Matching UUID)."
                # Falls through
            else:
                print "WARN: Image %s was not identified. Double-check you have the correct image ID _OR_ fix this script!" % image_id
                continue
            # Now that we have made it through the logic, fix!
            print "Fixing -- Creating a new application (by UUID) for providermachine."
            _apply_fix(accounts, provider, image_id)

def get_image(accounts, image_id):
    global Images
    if not Images:
        Images = accounts.image_manager.admin_list_images()
    found_images = [i for i in Images if i.id == image_id]
    if found_images:
        return found_images[0]
    return None

def _apply_fix(accounts, provider, image_id):
    uuid = _generate_app_uuid(image_id)
    g_img = get_image(accounts, image_id)
    name = g_img.name

    pm = ProviderMachine.objects.get(provider=provider, identifier=image_id)

    apps = Application.objects.filter(uuid=uuid)
    if not apps.count():
        app = create_application(image_id, provider.id, name, not image.is_public, uuid=uuid)
    else:
        app = apps[0]

    pm.application = app
    pm.save()
    print "Old properties: %s" % g_img.properties
    app.update_images()
    g_img = get_image(accounts, image_id)
    print "New properties: %s" % g_img.properties

def _identify_problem_apps(provider, accounts, print_log=True):
    """
    """
    name_match=[]
    new_apps = []
    not_real_apps = []
    correct_apps = []
    incorrect_apps = []

    for pm in ProviderMachine.objects.filter(provider=provider):
        uuid = _generate_app_uuid(pm.identifier)
        if print_log:
            print "ProviderMachine: %s == UUID: %s" % (pm.identifier, uuid)
        apps = Application.objects.filter(uuid=uuid)
        if not apps.count():
            # NO UUID Match on providermachine's identifier
            g_img = get_image(accounts, pm.identifier)
            if not g_img:
                if print_log:
                    print "DELETE: Image was deleted %s" % pm.identifier
                not_real_apps.append(pm)
                continue
            #Lookup by name
            name = g_img.name
            if Application.objects.filter(name=name).count():
                #Application name matches original image name
                apps_2 = Application.objects.filter(name=name)
                if apps_2[0] == pm.application:
                    #Matched name is ProviderMachines application
                    if print_log:
                        print "OKAY: %s points to correct application by NAME: %s" % (pm.identifier, name)
                    name_match.append(pm)
                else:
                    #Matched name is NOT ProviderMachines application
                    if print_log or True:
                        print "ProviderMachine: %s points to %s , doesnt match Name:%s OR UUID:%s" % (pm, pm.application, name, uuid)
                    incorrect_apps.append(pm)
            else:
                # No matching named application.. Print original machine and application
                if print_log:
                    print "CREATE: ProviderMachine: %s points to %s, Creating new application-%s and assigning to %s. Named %s based on glance image." % (pm.identifier, pm.application, uuid, pm.identifier, name)
                new_apps.append(pm)
        else:
            # UUID Match on providermachine's identifier
            if apps[0] == pm.application:
                if print_log:
                    print "OKAY: %s points to correct application." % pm.identifier
                correct_apps.append(pm)
            else:
                if print_log or True:
                    print "Application for UUID:%s - %s exists but PM points to %s" % (uuid, apps[0], pm.application)
                incorrect_apps.append(pm)
    return (not_real_apps, incorrect_apps, new_apps, name_match, correct_apps)


if __name__ == "__main__":
    main()
