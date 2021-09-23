import pyro
import torch
from functools import partial
import xarray as xr
from sfacts.pyro_util import all_torch, shape_info, set_random_seed
from sfacts.logging_util import info
from sfacts.data import World
from warnings import warn
from pprint import pformat


class Structure:
    def __init__(self, generative, dims, description, default_hyperparameters=None):
        """

        *generative* :: Pyro generative model function(shape_dim_0, shape_dim_1, shape_dim_2, ..., **hyper_parameters)
        *dims* :: Sequence of names for dim_0, dim_1, dim_2, ...
        *description* :: Mapping from model variable to its dims.
        *default_hyperparameters* :: Values to use for hyperparameters when not explicitly set.
        """
        if default_hyperparameters is None:
            default_hyperparameters = {}

        self.generative = generative
        self.dims = dims
        self.description = description
        self.default_hyperparameters = default_hyperparameters

    #         _ = self(self._dummy_shape, **all_torch(**self.default_hyperparameters))

    #         info(f"New Structure({self.generative}, {self.default_hyperparameters})")

    def __call__(self, shape, data, hyperparameters, unit):
        assert len(shape) == len(self.dims)
        conditioned_generative = pyro.condition(self.generative, data)
        return conditioned_generative(*shape, **hyperparameters, _unit=unit)

    #     def condition(self, **data):
    #         new_data = self.data.copy()
    #         new_data.update(data)
    #         return self.__class__(
    #             generative=self.generative,
    #             dims=self.dims,
    #             description=self.description,
    #             default_hyperparameters=self.default_hyperparameters,
    #             data=new_data,
    #         )

    @property
    def _dummy_shape(self):
        shape = range(1, len(self.dims) + 1)
        return shape

    def explain_shapes(self, shape=None):
        if shape is None:
            shape = self._dummy_shape
        info(dict(zip(self.dims, shape)))
        shape_info(self(shape, **self.default_hyperparameters))

    def __repr__(self):
        return (
            self.__class__.__name__ + "("
            + "generative=" + repr(self.generative.__qualname__)
            + ", " + "dims=" + repr(self.dims)
            + ", " + "description=" + repr(self.description)
            + ", " + "default_hyperparameters=" + repr(self.default_hyperparameters)
            + ")"
        )

    def pformat(self, indent=1):
        return (
            self.__class__.__name__ + "("
            + "\n" + " " * indent + " generative=" + self.generative.__qualname__
            + ",\n" + " " * indent + " dims=" + pformat(self.dims, indent=indent + 1)
            + ",\n" + " " * indent + " description=" + pformat(self.description, indent=indent + 1)
            + ",\n" + " " * indent + " default_hyperparameters=" + pformat(self.default_hyperparameters, indent=indent + 1)
            + "\n" + " " * (indent - 1) + ")"
        )


# For decorator use.
def structure(dims, description, default_hyperparameters=None):
    return partial(
        Structure,
        dims=dims,
        description=description,
        default_hyperparameters=default_hyperparameters,
    )


class ParameterizedModel:
    def __init__(
        self,
        structure,
        coords,
        dtype=torch.float32,
        device="cpu",
        data=None,
        hyperparameters=None,
    ):
        if hyperparameters is None:
            hyperparameters = {}

        if data is None:
            data = {}

        # Special case of alleles because they are format in different
        # ways for genotypes (0, 1) and metagenotypes (alt-count + total_count).
        if "allele" in coords:
            if "alt" in coords["allele"]:
                if list(coords["allele"]).index("alt") > 0:
                    warn(
                        "Weird things can happen if binary (alt/ref) allele coordinates are passed as ['ref', 'alt']."
                    )

        self.structure = structure
        self.coords = {k: self._coords_or_range(coords[k]) for k in self.structure.dims}
        self.dtype = dtype
        self.device = device
        self.hyperparameters = self.structure.default_hyperparameters.copy()
        self.hyperparameters.update(hyperparameters)
        self.data = data

    @property
    def sizes(self):
        return {k: len(self.coords[k]) for k in self.structure.dims}

    @property
    def shape(self):
        return tuple(self.sizes.values())

    def __repr__(self):
        return (
            self.__class__.__name__ + "("
            + "structure=" + repr(self.structure)
            + ", " + "coords=" + repr(self.coords)
            + ", " + "dtype=" + repr(self.dtype)
            + ", " + "device=" + repr(self.device)
            + ", " + "hyperparameters=" + repr(self.hyperparameters)
            + ", " + "data=" + repr(self.data)
            + ")"
        )

    def pformat(self, indent=1):
        return (
            self.__class__.__name__ + "("
            + "\n" + " " * indent + "structure=" + self.structure.pformat(indent=indent + 1)
            + ",\n" + " " * indent + "coords=" + pformat(self.coords, indent=indent + 1)
            + ",\n" + " " * indent + "dtype=" + pformat(self.dtype, indent=indent + 1)
            + ",\n" + " " * indent + "device=" + pformat(self.dtype, indent=indent + 1)
            + ",\n" + " " * indent + "hyperparameters=" + pformat(self.hyperparameters, indent=indent + 1)
            + ",\n" + " " * indent + "data=" + pformat(self.data, indent=indent + 1)
            + "\n" + " " * (indent - 1) + ")"
        )

    def __call__(self):
        # Here's where all the action happens.
        # All parameters are cast based on dtype and device.
        # The model is conditioned on the
        # data, and then called with the shape tuple
        # and cast hyperparameters.
        return self.structure(
            self.shape,
            data=all_torch(**self.data, dtype=self.dtype, device=self.device),
            hyperparameters=all_torch(
                **self.hyperparameters, dtype=self.dtype, device=self.device
            ),
            unit=torch.tensor(1.0, dtype=self.dtype, device=self.device),
        )

    @staticmethod
    def _coords_or_range(coords):
        if type(coords) == int:
            return range(coords)
        else:
            return coords

    def with_hyperparameters(self, **hyperparameters):
        new_hyperparameters = self.hyperparameters.copy()
        new_hyperparameters.update(hyperparameters)
        return self.__class__(
            structure=self.structure,
            coords=self.coords,
            dtype=self.dtype,
            device=self.device,
            hyperparameters=new_hyperparameters,
            data=self.data,
        )

    def with_amended_coords(self, **coords):
        new_coords = self.coords.copy()
        new_coords.update(coords)
        return self.__class__(
            structure=self.structure,
            coords=new_coords,
            dtype=self.dtype,
            device=self.device,
            hyperparameters=self.hyperparameters,
            data=self.data,
        )

    def condition(self, **data):
        new_data = self.data.copy()
        new_data.update(data)
        return self.__class__(
            structure=self.structure,
            coords=self.coords,
            dtype=self.dtype,
            device=self.device,
            hyperparameters=self.hyperparameters,
            data=new_data,
        )

    def format_world(self, data):
        out = {}
        for k in self.structure.description:
            out[k] = xr.DataArray(
                data[k],
                dims=self.structure.description[k],
                coords={dim: self.coords[dim] for dim in self.structure.description[k]},
            )
        return World(xr.Dataset(out))

    def simulate(self, n=1, seed=None):
        set_random_seed(seed)
        obs = pyro.infer.Predictive(self, num_samples=n)()
        obs = {k: obs[k].detach().cpu().numpy().squeeze() for k in obs.keys()}
        return obs

    def simulate_world(self, seed=None):
        return self.format_world(self.simulate(n=1))
