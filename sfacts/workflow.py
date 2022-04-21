import sfacts as sf
import time
import torch
import xarray as xr
import numpy as np
import pandas as pd


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
    metagenotype,
    nstrain,
    device="cpu",
    dtype=torch.float32,
):
    _info = lambda *args, **kwargs: sf.logging_util.info(*args, quiet=quiet, **kwargs)
    _phase_info = lambda *args, **kwargs: sf.logging_util.phase_info(
        *args, quiet=quiet, **kwargs
    )

    _info(
        f"START: NOT fitting {nstrain} strains with data shape {metagenotype.sizes}. (This workflow is a no-op.)",
    )
    with _phase_info(
        f"NOT fitting {nstrain} strains with data shape {metagenotype.sizes}."
    ):
        _info("(This workflow is a no-op for testing purposes.)")
        pmodel = sf.model.ParameterizedModel(
            structure,
            coords=dict(
                sample=metagenotype.sample.values,
                position=metagenotype.position.values,
                allele=metagenotype.allele.values,
                strain=range(nstrain),
            ),
            hyperparameters=hyperparameters,
            data=condition_on,
            device=device,
            dtype=dtype,
        ).condition(**metagenotype.to_counts_and_totals())


def fit_metagenotype_complex(
    structure,
    metagenotype,
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
        f"Fitting {nstrain} strains with data shape {metagenotype.sizes}."
    ):
        if nmf_init:
            with _phase_info("Initializing with NMF."):
                _info("(This may take a while if data dimensions are large.)")
                nmf_kwargs = DEFAULT_NMF_KWARGS.copy()
                nmf_kwargs.update(nmf_init_kwargs)
                approx = sf.estimation.nmf_approximation(
                    metagenotype.to_world(),
                    s=nstrain,
                    random_state=nmf_seed,
                    **nmf_kwargs,
                )
                initialize_params = dict(
                    gamma=approx.genotype.values,
                    pi=approx.community.values,
                )
        else:
            initialize_params = None

        with _phase_info(f"Fitting model parameters."):
            pmodel = sf.model.ParameterizedModel(
                structure,
                coords=dict(
                    sample=metagenotype.sample.values,
                    position=metagenotype.position.values,
                    allele=metagenotype.allele.values,
                    strain=range(nstrain),
                ),
                hyperparameters=hyperparameters,
                data=condition_on,
                device=device,
                dtype=dtype,
            ).condition(**metagenotype.to_counts_and_totals())

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
                sf.evaluation.metagenotype_error2(est_curr, metagenotype)[0]
            )
        )

    return est_curr, est_list, history_list


def iteratively_fit_genotype_conditioned_on_community(
    structure,
    metagenotype,
    community,
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

    nstrain = len(community.strain)
    nsample = len(community.sample)
    nposition_full = len(metagenotype.position)
    with _phase_info(f"Fitting genotype for {nposition_full} positions."):
        _info(
            f"Conditioned on provided community with {nstrain} strains and {nsample} samples."
        )
        nposition = min(nposition, nposition_full)

        metagenotype_full = metagenotype
        start_time = time.time()
        pmodel = sf.model.ParameterizedModel(
            structure,
            coords=dict(
                sample=community.sample.values,
                position=range(nposition),
                allele=metagenotype_full.allele.values,
                strain=community.strain.values,
            ),
            hyperparameters=hyperparameters,
            data=dict(
                pi=community.values,
            ),
            device=device,
            dtype=dtype,
        )

        _info("Iteratively fitting genotype by chunks.")
        genotype_chunks = []
        for position_start, position_end in _chunk_start_end_iterator(
            metagenotype_full.sizes["position"],
            nposition,
        ):
            with _phase_info(f"Chunk [{position_start}, {position_end})."):
                metagenotype_chunk = metagenotype_full.mlift(
                    "isel", position=slice(position_start, position_end)
                )
                est_curr, history = sf.estimation.estimate_parameters(
                    pmodel.with_amended_coords(
                        position=metagenotype_chunk.position.values,
                    ).condition(**metagenotype_chunk.to_counts_and_totals()),
                    quiet=quiet,
                    device=device,
                    dtype=dtype,
                    **estimation_kwargs,
                )
                genotype_chunks.append(est_curr.genotype.data)
                history_list.append(history)
                est_list.append(est_curr)

        with _phase_info(f"Concatenating chunks."):
            genotype = sf.data.Genotype(xr.concat(genotype_chunks, dim="position"))
            est_curr = sf.data.World(
                est_curr.data.drop_dims(["position", "allele"]).assign(
                    genotype=genotype.data,
                    metagenotype=metagenotype_full.data,
                )
            )
        est_list.append(est_curr)
    return est_curr, est_list, history_list


def evaluate_fit_against_simulation(sim, fit):
    # Re-indexing the simulation by the subset of positions and samples
    # that were actually fit.
    sim = sim.sel(position=fit.position.astype(int), sample=fit.sample.astype(int))

    mgen_error = sf.evaluation.metagenotype_error2(fit, discretized=True)
    fwd_genotype_error = sf.evaluation.discretized_weighted_genotype_error(sim, fit)
    rev_genotype_error = sf.evaluation.discretized_weighted_genotype_error(fit, sim)
    bc_error = sf.evaluation.braycurtis_error(sim, fit)
    unifrac_error = sf.evaluation.unifrac_error(sim, fit)
    entropy_error = sf.evaluation.community_entropy_error(sim, fit)

    return pd.Series(
        dict(
            mgen_error=mgen_error[0],
            fwd_genotype_error=fwd_genotype_error[0],
            rev_genotype_error=rev_genotype_error[0],
            bc_error=bc_error[0],
            unifrac_error=unifrac_error[0],
            entropy_error=entropy_error[0],
        )
    )
