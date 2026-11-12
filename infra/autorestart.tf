# Spot VMs are stopped (deallocated) when Azure evicts them and stay stopped.
# This Logic App calls "start" on the VM every 15 minutes (daytime only). Starting a VM that
# is already running does nothing, so the only effect is restarting it after
# an eviction. Cost is a few cents per month (Consumption plan).

resource "azurerm_logic_app_workflow" "vm_autostart" {
  count               = var.use_spot ? 1 : 0
  name                = "${var.name_prefix}-VM-AutoStart"
  location            = local.location
  resource_group_name = data.azurerm_resource_group.rg.name
  tags                = local.tags

  # The Logic App signs in to Azure as itself (no passwords or keys).
  identity {
    type = "SystemAssigned"
  }

  lifecycle {
    ignore_changes = [tags["CreationDateTime"], tags["Environment"]]
  }
}

# Every 15 minutes, 09:00-19:45 Israel time: covers the agent's 10:00, 16:00
# and 19:00 runs and leaves the VM alone at night (the course
# subscription shuts VMs down at 20:00, see the "scheduling" tag).
# A Logic App has one trigger, and its schedule is "these hours x these
# minutes", so the window is whole hours 9-16.
resource "azurerm_logic_app_trigger_recurrence" "every_15_minutes" {
  count        = var.use_spot ? 1 : 0
  name         = "every-15-minutes-daytime"
  logic_app_id = azurerm_logic_app_workflow.vm_autostart[0].id
  frequency    = "Day"
  interval     = 1
  time_zone    = "Israel Standard Time"

  schedule {
    at_these_hours   = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
    at_these_minutes = [0, 15, 30, 45]
  }
}

resource "azurerm_logic_app_action_custom" "start_vm" {
  count        = var.use_spot ? 1 : 0
  name         = "start-vm"
  logic_app_id = azurerm_logic_app_workflow.vm_autostart[0].id

  body = jsonencode({
    type = "Http"
    inputs = {
      method = "POST"
      uri    = "https://management.azure.com${azurerm_linux_virtual_machine.vm.id}/start?api-version=2024-07-01"
      authentication = {
        type     = "ManagedServiceIdentity"
        audience = "https://management.azure.com/"
      }
    }
    runAfter = {}
  })
}

# Allow the Logic App to start (and only manage) this one VM.
resource "azurerm_role_assignment" "autostart_vm" {
  count                = var.use_spot ? 1 : 0
  scope                = azurerm_linux_virtual_machine.vm.id
  role_definition_name = "Virtual Machine Contributor"
  principal_id         = azurerm_logic_app_workflow.vm_autostart[0].identity[0].principal_id
}
