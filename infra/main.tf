terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
  # The account cannot register resource providers in this subscription.
  resource_provider_registrations = "none"
}

# Existing resource group: read only, never created or modified here.
data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

locals {
  location = data.azurerm_resource_group.rg.location
  tags     = { Team = "Microsoft" }
}

resource "azurerm_virtual_network" "vnet" {
  name                = "${var.name_prefix}-VNet"
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = local.location
  address_space       = ["10.40.0.0/16"]
  tags                = local.tags

  # Azure Policy adds these tags automatically; don't fight it.
  lifecycle {
    ignore_changes = [tags["CreationDateTime"], tags["Environment"], tags["scheduling"]]
  }
}

resource "azurerm_subnet" "subnet" {
  name                 = "${var.name_prefix}-Subnet"
  resource_group_name  = data.azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.40.1.0/24"]
}

resource "azurerm_network_security_group" "nsg" {
  name                = "${var.name_prefix}-NSG"
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = local.location
  tags                = local.tags

  # Azure Policy adds these tags automatically; don't fight it.
  lifecycle {
    ignore_changes = [tags["CreationDateTime"], tags["Environment"], tags["scheduling"]]
  }

  # SSH only from the owner's public IP. Nothing else is open inbound.
  security_rule {
    name                       = "allow-ssh-from-my-ip"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "${var.my_ip}/32"
    destination_address_prefix = "*"
  }
}

resource "azurerm_public_ip" "pip" {
  name                = "${var.name_prefix}-PublicIP"
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = local.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags

  # Azure Policy adds these tags automatically; don't fight it.
  lifecycle {
    ignore_changes = [tags["CreationDateTime"], tags["Environment"], tags["scheduling"]]
  }
}

resource "azurerm_network_interface" "nic" {
  name                = "${var.name_prefix}-NIC"
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = local.location
  tags                = local.tags

  # Azure Policy adds these tags automatically; don't fight it.
  lifecycle {
    ignore_changes = [tags["CreationDateTime"], tags["Environment"], tags["scheduling"]]
  }

  ip_configuration {
    name                          = "ipconfig1"
    subnet_id                     = azurerm_subnet.subnet.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.pip.id
  }
}

resource "azurerm_network_interface_security_group_association" "nic_nsg" {
  network_interface_id      = azurerm_network_interface.nic.id
  network_security_group_id = azurerm_network_security_group.nsg.id
}

resource "azurerm_linux_virtual_machine" "vm" {
  name                            = "${var.name_prefix}-VM"
  resource_group_name             = data.azurerm_resource_group.rg.name
  location                        = local.location
  size                            = var.vm_size
  admin_username                  = var.admin_username
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.nic.id]
  tags                            = local.tags

  # Azure Policy adds these tags automatically; don't fight it.
  lifecycle {
    ignore_changes = [tags["CreationDateTime"], tags["Environment"], tags["scheduling"]]
  }

  # Azure Spot: much cheaper, but Azure may evict (stop) the VM when it needs
  # the capacity. "Deallocate" keeps the disk; start the VM again afterwards.
  # max_bid_price = -1 means "pay up to the regular price, never evict for price".
  priority        = var.use_spot ? "Spot" : "Regular"
  eviction_policy = var.use_spot ? "Deallocate" : null
  max_bid_price   = var.use_spot ? -1 : null

  admin_ssh_key {
    username   = var.admin_username
    public_key = file(pathexpand(var.ssh_public_key_path))
  }

  os_disk {
    name                 = "${var.name_prefix}-OSDisk"
    caching              = "ReadWrite"
    storage_account_type = "StandardSSD_LRS"
    disk_size_gb         = 30
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server-gen1" # Av2 sizes only support Generation 1 VMs
    version   = "latest"
  }
}
