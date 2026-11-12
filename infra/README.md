# Azure Linux VM with Terraform

Creates one Ubuntu 24.04 LTS VM in an **existing** Azure resource group, with:

- its own virtual network and subnet
- a network security group that allows SSH (port 22) **only from your IP**
- a static public IP
- SSH-key login only (no password)
- Azure Spot pricing (much cheaper; Azure may stop the VM when it needs capacity)
- an auto-restart for Spot evictions: a small Logic App that starts the VM
  every 15 minutes between 09:00 and 19:45 Israel time (starting a running VM
  does nothing)
- all resources tagged `Team=Microsoft`

The resource group is only read (`data` block), never created or deleted.

## Requirements

- [Terraform](https://developer.hashicorp.com/terraform/install) 1.6+
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), signed in: `az login`
- An SSH key pair, e.g. `ssh-keygen -t ed25519 -f ~/.ssh/vm-microsoft`

## Usage

```
cp terraform.tfvars.example terraform.tfvars   # then edit your values
terraform init                                  # once per folder / provider change
terraform plan -out tfplan                      # dry run: shows what will be created
terraform apply tfplan                          # creates the resources
terraform destroy                               # removes everything this created
```

After `apply`, Terraform prints the VM's public IP and an `ssh` command.

## Files

| File | What it is |
|---|---|
| `main.tf` | Provider, resource group lookup, network, NSG, public IP, NIC, VM |
| `variables.tf` | Input definitions (with defaults where they are the same for everyone) |
| `autorestart.tf` | Logic App + managed identity that restarts the Spot VM after an eviction |
| `outputs.tf` | Public IP and SSH command |
| `terraform.tfvars.example` | Template for your own values (copy to `terraform.tfvars`) |

`terraform.tfvars`, state files and `.terraform/` are git-ignored on purpose.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `subscription_id` | (required) | Azure subscription to deploy into |
| `resource_group_name` | `rg-microsoft` | Existing resource group |
| `my_ip` | (required) | Your public IPv4; only it may SSH in |
| `vm_size` | `Standard_A2_v2` | 2 vCPU / 4 GiB |
| `admin_username` | `azureuser` | Linux admin user |
| `ssh_public_key_path` | `~/.ssh/vm-microsoft.pub` | Public key installed on the VM |
| `use_spot` | `true` | Spot VM + auto-restart; `false` for a regular VM (no auto-restart) |
| `name_prefix` | `Microsoft` | Used in all resource names, e.g. `Microsoft-VM`, `Microsoft-VNet` |

If your home IP changes, update `my_ip` and run `plan` / `apply` again.

## Notes

- `Standard_A2_v2` needs a Gen1 image, so the VM uses `server-gen1`.
- Tags that an Azure Policy adds on its own (`CreationDateTime`, `Environment`,
  `scheduling`) are ignored by Terraform, so `plan` stays clean.
- The auto-restart window is set in `autorestart.tf` (`at_these_hours`).
