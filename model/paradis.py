"""Physically inspired neural architecture for the weather forecasting model."""

import torch
from torch import nn

from model.clp_block import CLP
from model.clp_variational import VariationalCLP
from model.padding import GeoCyclicPadding

from typing import Tuple, Union


class NeuralSemiLagrangian(nn.Module):
    """Implements the semi-Lagrangian advection."""

    def __init__(self, hidden_dim: int, mesh_size: tuple, variational: bool):
        super().__init__()

        # For cubic interpolation
        self.padding = 1
        self.padding_interp = GeoCyclicPadding(self.padding)
        self.hidden_dim = hidden_dim

        # Flag for variational variant to be used in forward
        self.variational = variational

        # Neural network that will learn an effective velocity along the trajectory
        # Output 2 channels per hidden dimension for u and v
        if not self.variational:
            self.velocity_net = CLP(hidden_dim, 2 * hidden_dim, mesh_size)
        else:
            self.velocity_net = VariationalCLP(hidden_dim, 2 * hidden_dim, mesh_size)

    def _transform_to_latlon(
        self,
        lat_prime: torch.Tensor,
        lon_prime: torch.Tensor,
        lat_p: torch.Tensor,
        lon_p: torch.Tensor,
    ) -> tuple:
        """Transform from local rotated coordinates back to standard latlon coordinates."""
        # Pre-compute trigonometric functions
        sin_lat_prime = torch.sin(lat_prime)
        cos_lat_prime = torch.cos(lat_prime)
        sin_lon_prime = torch.sin(lon_prime)
        cos_lon_prime = torch.cos(lon_prime)
        sin_lat_p = torch.sin(lat_p)
        cos_lat_p = torch.cos(lat_p)

        # Compute standard latitude
        sin_lat = sin_lat_prime * cos_lat_p + cos_lat_prime * cos_lon_prime * sin_lat_p
        lat = torch.arcsin(torch.clamp(sin_lat, -1 + 1e-7, 1 - 1e-7))

        # Compute standard longitude
        num = cos_lat_prime * sin_lon_prime
        den = cos_lat_prime * cos_lon_prime * cos_lat_p - sin_lat_prime * sin_lat_p

        lon = lon_p + torch.atan2(num, den)

        # Normalize longitude to [0, 2π]
        lon = torch.remainder(lon + 2 * torch.pi, 2 * torch.pi)

        return lat, lon

    def forward(
        self,
        hidden_features: torch.Tensor,
        lat_grid: torch.Tensor,
        lon_grid: torch.Tensor,
        dt: float,
    ) -> Union[torch.Tensor, Tuple[torch.Tensor,torch.Tensor]]:
        """Compute advection using rotated coordinate system."""
        batch_size = hidden_features.shape[0]
        kl_loss = torch.tensor(0.0)

        # Get learned velocities for each channel
        if self.variational:
            velocities, kl_loss = self.velocity_net(hidden_features)
        else:
            velocities = self.velocity_net(hidden_features)

        # Reshape velocities to separate u,v components per channel
        # [batch, 2*hidden_dim, lat, lon] -> [batch, hidden_dim, 2, lat, lon]
        velocities = velocities.view(
            batch_size, 2, self.hidden_dim, *velocities.shape[-2:]
        )

        # Extract u,v components
        u = velocities[:, 0]  # [batch, hidden_dim, lat, lon]
        v = velocities[:, 1]

        # Compute departure points in a local rotated coordinate system in which the origin
        # of latitude and longitude is moved to the arrival point
        lon_prime = -u * dt
        lat_prime = -v * dt

        # Transform from rotated coordinates back to standard coordinates
        # Expand lat/lon grid for broadcasting with per-channel coordinates
        lat_grid = lat_grid.unsqueeze(1).expand(-1, self.hidden_dim, -1, -1)
        lon_grid = lon_grid.unsqueeze(1).expand(-1, self.hidden_dim, -1, -1)

        # Get the max and min values for normalization
        min_lat = torch.min(lat_grid)
        max_lat = torch.max(lat_grid)

        min_lon = torch.min(lon_grid)
        max_lon = torch.max(lon_grid)

        lat_dep, lon_dep = self._transform_to_latlon(
            lat_prime, lon_prime, lat_grid, lon_grid
        )

        grid_x = 2 * (lon_dep - min_lon) / (max_lon - min_lon) - 1
        grid_y = 2 * (lat_dep - min_lat) / (max_lat - min_lat) - 1

        # Apply periodicity for outside values along longitude set to [-1, 1]
        grid_x = torch.remainder(grid_x + 1, 2) - 1

        # Apply geocyclic longitude roll for values beyond +/-90 degrees latitude
        geo_mask_left = grid_x <= 0
        geo_mask_right = grid_x > 0
        lat_mask_outer = torch.abs(grid_y) > 1
        grid_x = torch.where(lat_mask_outer & geo_mask_left, grid_x + 1, grid_x)
        grid_x = torch.where(lat_mask_outer & geo_mask_right, grid_x - 1, grid_x)

        # Mirror values outside of the range [-1, 1] along the latitude direction
        grid_y = torch.where(grid_y < -1, -(2 + grid_y), grid_y)
        grid_y = torch.where(grid_y > 1, 2 - grid_y, grid_y)

        # Reshape grid coordinates for interpolation
        # [batch, dynamic_channels, lat, lon] -> [batch*dynamic_channels, lat, lon]
        grid_x = grid_x.view(batch_size * self.hidden_dim, *grid_x.shape[-2:])
        grid_y = grid_y.view(batch_size * self.hidden_dim, *grid_y.shape[-2:])

        # Apply padding and reshape hidden features
        dynamic_padded = self.padding_interp(hidden_features)

        # Make sure interpolation remains in right range after padding
        grid_x = grid_x * hidden_features.size(-1) / dynamic_padded.size(-1)
        grid_y = grid_y * hidden_features.size(-2) / dynamic_padded.size(-2)

        # Create interpolation grid
        grid = torch.stack([grid_x, grid_y], dim=-1)

        # Apply padding and reshape hidden features
        dynamic_padded = self.padding_interp(hidden_features)

        # Apply padding and reshape features
        dynamic_padded = dynamic_padded.reshape(
            batch_size * self.hidden_dim, 1, *dynamic_padded.shape[-2:]
        )

        # Interpolate
        interpolated = torch.nn.functional.grid_sample(
            dynamic_padded,
            grid,
            align_corners=True,
            mode="bicubic",
            padding_mode="border",
        )

        # Reshape back to original dimensions
        interpolated = interpolated.view(
            batch_size, self.hidden_dim, *interpolated.shape[-2:]
        )

        if self.variational:
            return interpolated, kl_loss

        return interpolated


class Paradis(nn.Module):
    """Weather forecasting model main class."""

    # Synoptic time scale (~1/Ω) in seconds
    SYNOPTIC_TIME_SCALE = 7.29212e5

    def __init__(self, datamodule, cfg):
        super().__init__()

        # Extract dimensions from config
        output_dim = datamodule.num_out_features
        mesh_size = (datamodule.lat_size, datamodule.lon_size)
        num_levels = len(cfg.features.pressure_levels)
        self.num_common_features = datamodule.num_common_features
        self.variational = cfg.ensemble.enable

        # Get channel sizes
        self.num_dynamic_channels = len(datamodule.dataset.dyn_input_features) + len(
            cfg.features.input.forcings
        )
        self.num_static_channels = len(cfg.features.input.constants)
        self.num_input_channels = datamodule.dataset.num_in_features

        hidden_dim = cfg.model.hidden_multiplier * self.num_dynamic_channels

        # Input projection for combined dynamic and static features
        self.input_proj = CLP(self.num_input_channels, hidden_dim, mesh_size)

        # Rescale the time step to a fraction of a synoptic time scale
        self.num_substeps = cfg.model.num_substeps
        self.dt = cfg.model.base_dt / self.SYNOPTIC_TIME_SCALE / self.num_substeps

        # Advection layer
        self.advection = NeuralSemiLagrangian(hidden_dim, mesh_size, self.variational)

        # Diffusion-reaction layer
        self.diffusion_reaction = CLP(
            hidden_dim, hidden_dim, mesh_size, pointwise_conv=True
        )

        # Output projection
        self.output_proj = nn.Sequential(
            GeoCyclicPadding(1),
            nn.Conv2d(hidden_dim, output_dim, kernel_size=3),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:

        # Extract lat/lon from static features (last 2 channels)
        x_static = x[:, self.num_dynamic_channels :]
        lat_grid = x_static[:, -2, :, :]
        lon_grid = x_static[:, -1, :, :]

        # Project features to latent space
        z = self.input_proj(x)

        # Keep a copy for the residual projection
        z0 = z.clone()

        # Compute advection and diffusion-reaction
        for i in range(self.num_substeps):
            # Advect the features in latent space using a Semi-Lagrangian step
            z_adv = self.advection(z, lat_grid, lon_grid, self.dt)

            # Compute the diffusion residual
            dz = self.diffusion_reaction(z_adv)

            # Update the latent space features
            z += z_adv + self.dt * dz

        # Return a scaled residual formulation
        return x[:, : self.num_common_features] + self.output_proj(z - z0)
