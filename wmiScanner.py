# Core imports
import os

# Custom Imports
import wmi

# Local imports
from networkMngr import netDeviceTest, getComputers, getDeviceNetwork

# Database info
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()

# DB models and exceptions
from data import models
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from excepts import AlreadyInDB, SilentFail

Byte2GB = 1024 * 1024 * 1024
local = wmi.WMI()


def getWMIObjs(users, search=getDeviceNetwork()[2]):
    """Given ip range search and list of dictionary users: Returns a list of WMIObjects"""
    wmiObjs = []
    for ip, login in [(ip, login) for ip in getComputers(search) for login in users]:
        print("Trying to connect to", ip, "with user '" + login['user'] + "'")
        try:
            wmiObj = wmi.WMI(str(ip), user=login['user'], password=login['pass'])
        except wmi.x_wmi as e:
            # This is unfortunately the way this must be done. There is no error codes in wmi library AFAIK
            if e.com_error.excepinfo[2] == 'The RPC server is unavailable. ':
                raise EnvironmentError("Computer does not have WMI enabled")
            else:
                raise wmi.x_wmi(e.com_error.excepinfo[2])
        except IndexError:
            raise IndexError("Config file has errors. Likely is unmatching user/password combo")
        else:
            wmiObjs.append(wmiObj)
            break
    return wmiObjs  # Return credentials that worked in future. This will be a ID for the credential in DB


def WMIInfo(wmiObj=None, silentlyFail=False, skipUpdate=False):
    """Given wmiObj and bool settings silentlyFail and skipUpdate find information and store it in the database.
    wmiObj default is None. If none, the local WMI object will be used"""
    if not wmiObj:
        wmiObj = local

    """Get a list of valid network devices to use later to find MAC in DB"""
    netdevices = list(filter(
        lambda net: netDeviceTest(net),
        wmiObj.Win32_NetworkAdapter()
    ))
    if not netdevices:
        if silentlyFail:
            raise SilentFail(
                wmiObj.Win32_ComputerSystem()[-1].Name + " does not have any valid network devices."
            )
        else:
            raise LookupError(
                wmiObj.Win32_ComputerSystem()[-1].Name + " does not have any valid network devices."
            )

    """Setup machine and compModel to start import data into it"""
    machine, compModel = None, None
    for macaddr in netdevices:
        try:
            machine = models.Network.objects.get(mac=macaddr.MACAddress).machine  # Gets machine with mac address
        except ObjectDoesNotExist:
            machine, compModel = models.Machine(), models.MachineModel()
        except MultipleObjectsReturned:
            if silentlyFail:
                raise SilentFail("You have a duplicate machine in your database!")
            else:
                raise MultipleObjectsReturned("You have a duplicate machine in your database!")
        else:
            if skipUpdate:
                raise AlreadyInDB(machine.name, "is already in your database. Skipping")
            else:
                print(machine.name + " will be updated in the local database")
                compModel = machine.model  # Error handling needed if machine has no model
                break  # Machine has been found and defined. Update the machine
    # Need to add a way to make sure that a computer isn't going to replace another with matching mac
    if not machine:
        if silentlyFail:
            raise SilentFail("None of the network cards found have a mac address!")
        else:
            raise LookupError("None of the network cards found have a mac address!")

    """Begin creation of compModel"""
    modelName = wmiObj.Win32_ComputerSystem()[-1].Model.strip()
    modelManufacturer = wmiObj.Win32_ComputerSystem()[-1].Manufacturer.strip()
    if not machine.name:
        print(wmiObj.Win32_ComputerSystem()[-1].Name + " will be created in the local database")
        compModel, _ = models.MachineModel.objects.get_or_create(name=compModel.name,
                                                                 manufacturer=compModel.manufacturer)
    else:
        compModel.name = modelName
        compModel.manufacturer = modelManufacturer

    # The following will not only get compType, but also get the roles of the machine
    try:
        machine.roles = list(map(
            lambda server: models.Role.objects.get_or_create(name=server.Name.strip())[0],
            wmiObj.Win32_ServerFeature()
        ))
    except AttributeError:
        try:
            if wmiObj.Win32_Battery()[-1].BatteryStatus > 0:
                machine.compType = models.MachineModel.LAPTOP
        except IndexError:
            machine.compType = models.MachineModel.DESKTOP
    else:
        machine.compType = models.MachineModel.SERVER
    compModel.save()

    """Begin creation of machine"""
    machine.name = wmiObj.Win32_ComputerSystem()[-1].Name
    machine.os = wmiObj.Win32_OperatingSystem()[-1].Caption.strip()
    machine.model = compModel
    machine.save()

    """Begin creation of CPUModel"""
    def createCPU(cpu):
        cpuMod, _ = models.CPUModel.objects.get_or_create(
            name=cpu.Name.strip(),
            manufacturer=cpu.Manufacturer,
            partnum=cpu.PartNumber.strip(),
            arch=cpu.Architecture,
            family=cpu.Family,
            upgradeMethod=cpu.UpgradeMethod,
            cores=cpu.NumberOfCores,
            threads=cpu.ThreadCount,
            speed=cpu.MaxClockSpeed
        )
        cpuMod.save()

        processor = models.CPU(
            machine=machine,
            model=cpuMod,
            serial=cpu.SerialNumber.strip(),
            location=cpu.DeviceID
        )
        processor.save()

    list(map(
        lambda cpu: createCPU(cpu),
        filter(
            lambda processor: processor.ProcessorType == 3,
            wmiObj.Win32_Processor()
        )
    ))

    """Begin creation of RAM"""
    def createRAM(ram):
        ramMod, _ = models.RAMModel.objects.get_or_create(
            size=int(ram.Capacity),
            manufacturer=ram.Manufacturer,
            partnum=ram.PartNumber.strip(),
            speed=ram.Speed,
            formFactor=ram.FormFactor,
            memoryType=ram.MemoryType
        )
        ramMod.save()

        ramStick = models.RAM(
            machine=machine,
            model=ramMod,
            serial=ram.SerialNumber.strip(),
            location=ram.DeviceLocator
        )
        ramStick.save()

    list(map(
        lambda ram: createRAM(ram),
        wmiObj.Win32_PhysicalMemory()
    ))

    """Begin creation of Physical and Logical Disks
    Matching of Physical and Logical disks ported from:
    blogs.technet.microsoft.com/heyscriptingguy/2005/05/23/how-can-i-correlate-logical-drives-and-physical-disks/
    Thanks, ScriptingGuy1"""
    def makeQuery(FromWinClass, DeviceID, WhereWinClass):
        return 'ASSOCIATORS OF {' + FromWinClass + '.DeviceID="' + DeviceID + '"} WHERE AssocClass = ' + WhereWinClass

    def createDrive(PhysDrive):
        pdMod, _ = models.PhysicalDiskModel.objects.get_or_create(
            name=PhysDrive.Model,
            size=PhysDrive.Size,
            interface=PhysDrive.InterfaceType,
            manufacturer=PhysDrive.Manufacturer
        )
        pdMod.save()

        pd = models.PhysicalDisk(
            machine=machine,
            model=pdMod,
            serial=PhysDrive.SerialNumber,
            partitions=PhysDrive.Partitions,
        )
        pd.save()
        return pd

    for diskdrive in wmiObj.Win32_DiskDrive():
        """Get info from Win32_LogicalDisk"""
        physDisk = createDrive(diskdrive)
        partsOnDrive = makeQuery("Win32_DiskDrive", diskdrive.DeviceID, "Win32_DiskDriveToDiskPartition")
        for diskpart in wmiObj.query(partsOnDrive):
            diskPartToLogic = makeQuery("Win32_DiskPartition", diskpart.DeviceID, "Win32_LogicalDiskToPartition")
            for logicdisk in wmiObj.query(diskPartToLogic):
                """Get info from Win32_LogicalDisk"""
                logicDisk = models.LogicalDisk(
                    disk=physDisk,
                    name=logicdisk.Name,
                    mount=logicdisk.DeviceID,
                    filesystem=logicdisk.FileSystem,
                    size=logicdisk.Size,
                    freesize=logicdisk.FreeSpace,
                    type=logicdisk.DriveType
                )
                logicDisk.save()

    """Begin creation of GPU"""
    def createGPU(gpu):
        gpuMod, _ = models.GPUModel.objects.get_or_create(
            name=gpu.Name.strip(),
            size=int(gpu.AdapterRAM),
            refresh=gpu.MaxRefreshRate,
            arch=gpu.VideoArchitecture,
            memoryType=gpu.VideoMemoryType
        )
        gpuMod.save()

        gpuCard = models.GPU(
            machine=machine,
            model=gpuMod,
            location=gpu.DeviceID,
        )
        gpuCard.save()

    list(map(
        lambda gpu: createGPU(gpu),
        wmiObj.Win32_VideoController()
    ))

    """Begin creation of Network"""
    def createNetwork(net):
        netMod, _ = models.NetworkModel.objects.get_or_create(
            name=net.Name.strip(),
            manufacturer=net.Manufacturer
        )
        netMod.save()

        netCard = models.Network(
            machine=machine,
            model=netMod,
            mac=net.MACAddress,
            location=net.DeviceID,
        )
        netCard.save()

    list(map(
        lambda net: createNetwork(net),
        netdevices
    ))
    return machine
