variable "subscription_id" {
  description = "Azure subscription ID to deploy into."
  type        = string
}

variable "resource_group_name" {
  description = "Existing resource group (read only)."
  type        = string
  default     = "rg-microsoft"
}

variable "my_ip" {
  description = "Your public IPv4 address; only it may connect over SSH."
  type        = string
}

variable "vm_size" {
  description = "VM size (this subscription allows the Av2 series)."
  type        = string
  default     = "Standard_A2_v2"
}

variable "admin_username" {
  description = "Linux admin user."
  type        = string
  default     = "azureuser"
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key used to log in."
  type        = string
  default     = "~/.ssh/vm-microsoft.pub"
}

variable "use_spot" {
  description = "Run the VM as an Azure Spot VM (large discount, can be evicted)."
  type        = bool
  default     = true
}

variable "name_prefix" {
  description = "Prefix used in every resource name (e.g. <name_prefix>-VM)."
  type        = string
  default     = "Microsoft"
}
