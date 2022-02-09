import sfacts as sf
import time
import torch
import xarray as xr
import numpy as np


DEFAULT_NMF_KWARGS = dict(
    alpha=0.0,
    solver="cd",
    tol=1e-3,
    eps=1e-4,
)


def _chunk_start_end_iterator(total, per):
    for i in range(total // per):
        yield (per * i), (per * (i + 1))
    if (i + 1) * per < total:
        yield (i + 1) * per, total


def simulate_world(
    structure,
    sizes,
    hyperparameters,
    seed=None,
    data=None,
    dtype=torch.float32,
    device="cpu",
):
    if data is None:
        data = {}

    assert len(sizes) == 3, "Sizes should only be for strain, sample, and position."
    model = sf.model.ParameterizedModel(
        structure=structure,
        coords=dict(
            strain=sizes["strain"],
            sample=sizes["sample"],
            position=sizes["position"],
            allele=["alt", "ref"],
        ),
        hyperparameters=hyperparameters,
        dtype=dtype,
        device=device,
        data=data,
    )
    world = model.simulate_world(seed=seed)

    return model, world


def setup_model_but_do_nothing(
    structure,
    metagenotypes,
    nstrain,
    device="cpu",
    dtype=torch.float32,
):
    _info = lambda *args, **kwargs: sf.logging_util.info(*args, quiet=quiet, **kwargs)
    _phase_info = lambda *args, **kwargs: sf.logging_util.phase_info(
        *args, quiet=quiet, **kwargs
    )

    _info(
        f"START: NOT fitting {nstrain} strains with data shape {metagenotypes.sizes}. (This workflow is a no-op.)",
    )
    with _phase_info(
        f"NOT fitting {nstrain} strains with data shape {metagenotypes.sizes}."
    ):
        _info("(This workflow is a no-op for testing purposes.)")
        pmodel = sf.model.ParameterizedModel(
            structure,
            coords=dict(
                sample=metagenotypes.sample.values,
                position=metagenotypes.position.values,
                allele=metagenotypes.allele.values,
                strain=range(nstrain),
            ),
            hyperparameters=hyperparameters,
            data=condition_on,
            device=device,
            dtype=dtype,
        ).condition(**metagenotypes.to_counts_and_totals())


def fit_metagenotypes_complex(
    structure,
    metagenotypes,
    nstrain,
    hyperparameters=None,
    anneal_hyperparameters=None,
    annealiter=0,
    condition_on=None,
    device="cpu",
    dtype=torch.float32,
    quiet=False,
    nmf_init=False,
    nmf_init_kwargs=None,
    nmf_seed=None,
    estimation_kwargs=None,
):

    _info = lambda *args, **kwargs: sf.logging_util.info(*args, quiet=quiet, **kwargs)
    _phase_info = lambda *args, **kwargs: sf.logging_util.phase_info(
        *args, quiet=quiet, **kwargs
    )

    if estimation_kwargs is None:
        estimation_kwargs = {}
    if nmf_init_kwargs is None:
        nmf_init_kwargs = {}

    est_list = []
    history_list = []

    with _phase_info(
        f"Fitting {nstrain} strains with data shape {metagenotypes.sizes}."
    ):
        if nmf_init:
            with _phase_info("Initializing with NMF."):
                _info("(This may take a while if data dimensions are large.)")
                nmf_kwargs = DEFAULT_NMF_KWARGS.copy()
                nmf_kwargs.update(nmf_init_kwargs)
                approx = sf.estimation.nmf_approximation(
                    metagenotypes.to_world(),
                    s=nstrain,
                    random_state=nmf_seed,
                    **nmf_kwargs,
                )
                initialize_params = dict(
                    gamma=approx.genotypes.values,
                    pi=approx.communities.values,
                )
        else:
            initialize_params = None

        with _phase_info(f"Fitting model parameters."):
            pmodel = sf.model.ParameterizedModel(
                structure,
                coords=dict(
                    sample=metagenotypes.sample.values,
                    position=metagenotypes.position.values,
                    allele=metagenotypes.allele.values,
                    strain=range(nstrain),
                ),
                hyperparameters=hyperparameters,
                data=condition_on,
                device=device,
                dtype=dtype,
            ).condition(**metagenotypes.to_counts_and_totals())

            est_curr, history = sf.estimation.estimate_parameters(
                pmodel,
                quiet=quiet,
                device=device,
                dtype=dtype,
                anneal_hyperparameters=anneal_hyperparameters,
                annealiter=annealiter,
                initialize_params=initialize_params,
                **estimation_kwargs,
            )
            history_list.append(history)
            est_list.append(est_curr)
        _info(
            "Average metagenotype error: {}".format(
            sf.evaluation.metagenotype_error2(est_curr, metagenotypes)[0]
            )
        )

    return est_curr, est_list, history_list


def iteratively_fit_genotypes_conditioned_on_communities(
    structure,
    metagenotypes,
    communities,
    nposition,
    hyperparameters=None,
    condition_on=None,
    device="cpu",
    dtype=torch.float32,
    quiet=False,
    estimation_kwargs=None,
):

    _info = lambda *args, **kwargs: sf.logging_util.info(*args, quiet=quiet, **kwargs)
    _phase_info = lambda *args, **kwargs: sf.logging_util.phase_info(
        *args, quiet=quiet, **kwargs
    )

    if estimation_kwargs is None:
        estimation_kwargs = {}

    est_list = []
    history_list = []

    nstrain = len(communities.strain)
    nsample = len(communities.sample)
    nposition_full = len(metagenotypes.position)
    with _phase_info(f"Fitting genotypes for {nposition_full} positions."):
        _info(
            f"Conditioned on provided communities with {nstrain} strains and {nsample} samples."
        )
        nposition = min(nposition, nposition_full)

        metagenotypes_full = metagenotypes
        start_time = time.time()
        pmodel = sf.model.ParameterizedModel(
            structure,
            coords=dict(
                sample=communities.sample.values,
                position=range(nposition),
                allele=metagenotypes_full.allele.values,
                strain=communities.strain.values,
            ),
            hyperparameters=hyperparameters,
            data=dict(
                pi=communities.values,
            ),
            device=device,
            dtype=dtype,
        )

        _info("Iteratively fitting genotypes by chunks.")
        genotypes_chunks = []
        for position_start, position_end in _chunk_start_end_iterator(
            metagenotypes_full.sizes["position"],
            nposition,
        ):
            with _phase_info(f"Chunk [{position_start}, {position_end})."):
                metagenotypes_chunk = metagenotypes_full.mlift(
                    "isel", position=slice(position_start, position_end)
                )
                est_curr, history = sf.estimation.estimate_parameters(
                    pmodel.with_amended_coords(
                        position=metagenotypes_chunk.position.values,
                    ).condition(**metagenotypes_chunk.to_counts_and_totals()),
                    quiet=quiet,
                    device=device,
                    dtype=dtype,
                    **estimation_kwargs,
                )
                genotypes_chunks.append(est_curr.genotypes.data)
                history_list.append(history)
                est_list.append(est_curr)

        with _phase_info(f"Concatenating chunks."):
            genotypes = sf.data.Genotypes(xr.concat(genotypes_chunks, dim="position"))
            est_curr = sf.data.World(
                est_curr.data.drop_dims(["position", "allele"]).assign(
                    genotypes=genotypes.data,
                    metagenotypes=metagenotypes_full.data,
                )
            )
        est_list.append(est_curr)
    return est_curr, est_list, history_list
