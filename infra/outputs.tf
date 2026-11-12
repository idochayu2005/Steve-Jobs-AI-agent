output "public_ip" {
  description = "Public IP of the VM."
  value       = azurerm_public_ip.pip.ip_address
}

output "ssh_command" {
  description = "Command to connect to the VM."
  value       = "ssh -i ${trimsuffix(var.ssh_public_key_path, ".pub")} ${var.admin_username}@${azurerm_public_ip.pip.ip_address}"
}
