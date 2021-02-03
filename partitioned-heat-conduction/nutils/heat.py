#! /usr/bin/env python3

import nutils
import numpy
import treelog
import precice
from precice import action_read_iteration_checkpoint, action_write_iteration_checkpoint, action_write_initial_data

# The main function defines the parameter space for the script. Configurable
# parameters are the mesh density (in number of elements along an edge),
# element type (square, triangle, or mixed), type of basis function (std or
# spline, with availability depending on element type), and polynomial degree.


def main(side='Dirichlet', project=True):

    f = open('Error-' + side + '.log', "w+")

    for i in range(3, 4):
        nelems = 2**i
        cons, lhs, err = laplace(nelems=nelems, side=side, project=project)
        f.write('{:.2e}, {:.2e}\n'.format(1.0 / nelems, err))

    f.close()


def laplace(nelems: 'number of elements along edge' = 10, etype: 'type of elements (square/triangle/mixed)' = 'square',
            btype: 'type of basis function (std/spline)' = 'std', degree: 'polynomial degree' = 1, side='bottom', timestep=.1, project=True):

    # A unit square domain is created by calling the
    # :func:`nutils.mesh.unitsquare` mesh generator, with the number of elements
    # along an edge as the first argument, and the type of elements ("square",
    # "triangle", or "mixed") as the second. The result is a topology object
    # ``domain`` and a vectored valued geometry function ``geom``.

    print("Running utils")

    domain, geom = nutils.mesh.unitsquare(nelems, etype)

    if side == 'Neumann':
        domain = domain[:, :nelems // 2]
    elif side == 'Dirichlet':
        domain = domain[:, nelems // 2:]
    else:
        raise Exception('invalid side {!r}'.format(side))

    # To be able to write index based tensor contractions, we need to bundle all
    # relevant functions together in a namespace. Here we add the geometry ``x``,
    # a scalar ``basis``, and the solution ``u``. The latter is formed by
    # contracting the basis with a to-be-determined solution vector ``?lhs``.

    ns = nutils.function.Namespace()
    ns.diffusivity = 1
    ns.x = geom
    ns.basis = domain.basis(btype, degree=degree)
    ns.u = 'basis_n ?lhs_n'
    ns.dudt = 'basis_n (?lhs_n - ?lhs0_n) / ?dt'
    ns.flux = 'basis_n ?fluxdofs_n'
    ns.uexact = 'sin(x_0) cosh(x_1)'

    # We are now ready to implement the Laplace equation. In weak form, the
    # solution is a scalar field :math:`u` for which:
    #
    # .. math:: ∀ v: ∫_Ω v_{,k} u_{,k} - ∫_{Γ_n} v f = 0.
    #
    # By linearity the test function :math:`v` can be replaced by the basis that
    # spans its space. The result is an integral ``res`` that evaluates to a
    # vector matching the size of the function space.

    res = domain.integral(
        '(basis_n dudt + diffusivity basis_n,i u_,i) d:x' @ ns,
        degree=degree * 2)
    res -= domain.boundary['right'].integral(
        'basis_n cos(1) cosh(x_1) d:x' @ ns, degree=degree * 2)

    # The Dirichlet constraints are set by finding the coefficients that
    # minimize the error:
    #
    # .. math:: \min_u ∫_{\Gamma_d} (u - u_d)^2
    #
    # The resulting ``cons`` array holds numerical values for all the entries of
    # ``?lhs`` that contribute (up to ``droptol``) to the minimization problem.
    # All remaining entries are set to ``NaN``, signifying that these degrees of
    # freedom are unconstrained.

    sqr = domain.boundary['left'].integral('u^2 d:x' @ ns, degree=degree * 2)
    if side == 'top':
        sqr += domain.boundary['top'].integral(
            '(u - cosh(1) sin(x_0))^2 d:x' @ ns, degree=degree * 2)
    cons = nutils.solver.optimize('lhs', sqr, droptol=1e-15)

    # The unconstrained entries of ``?lhs`` are to be determined such that the
    # residual vector evaluates to zero in the corresponding entries. This step
    # involves a linearization of ``res``, resulting in a jacobian matrix and
    # right hand side vector that are subsequently assembled and solved. The
    # resulting ``lhs`` array matches ``cons`` in the constrained entries.

    configFileName = "../precice-config.xml"
    participantName = "Heat" + side
    meshNameGP = "MeshNutils-GP-" + side
    meshNameCC = "MeshNutils-CC-" + side
    solverProcessIndex = 0
    solverProcessSize = 1

    interface = precice.Interface(participantName, configFileName,
                                  solverProcessIndex, solverProcessSize)

    meshIDGP = interface.get_mesh_id(meshNameGP)
    meshIDCC = interface.get_mesh_id(meshNameCC)

    writeData = "Temperature" if side == "Neumann" else "Flux"
    readData = "Flux" if side == "Neumann" else "Temperature"

    writedataID = interface.get_data_id(writeData, meshIDCC)
    readdataID = interface.get_data_id(readData, meshIDGP)

    couplinginterface = domain.boundary['right' if side ==
                                        'Dirichlet' else 'left']
    couplingsampleGP = couplinginterface.sample('gauss', degree=degree * 2)
    couplingsampleCC = couplinginterface.sample(
        'uniform', 16)  # number of sub-samples

    verticesGP = couplingsampleGP.eval(ns.x).ravel()
    verticesCC = couplingsampleCC.eval(ns.x).ravel()

    dataIndicesGP = interface.set_mesh_vertices(meshIDGP,
                                                couplingsampleGP.npoints,
                                                verticesGP)
    dataIndicesCC = interface.set_mesh_vertices(meshIDCC,
                                                couplingsampleCC.npoints,
                                                verticesCC)

    precice_dt = interface.initialize()

    cons0 = cons
    res0 = res

    if project:
        projectionmatrix = couplinginterface.integrate(
            nutils.function.outer(ns.basis), degree=degree * 2)
        projectioncons = numpy.zeros(res.shape)
        projectioncons[projectionmatrix.rowsupp(1e-15)] = numpy.nan
        def fluxdofs(v): return projectionmatrix.solve(
            v, constrain=projectioncons)
    else:
        interfaceareas = couplinginterface.integrate(ns.basis, degree=degree)
        normalize = numpy.zeros_like(interfaceareas)
        normalize[interfaceareas > 0] = 1 / interfaceareas[interfaceareas > 0]
        def fluxdofs(v): return v * normalize

    lhs0 = numpy.zeros(res.shape)

    while interface.is_coupling_ongoing():

        if interface.is_read_data_available():
            readdata = interface.read_block_scalar_data(readdataID,
                                                        couplingsampleGP.npoints,
                                                        dataIndicesGP)
            coupledata = couplingsampleGP.asfunction(readdata)

            if side == 'Dirichlet':
                sqr = couplingsampleGP.integral((ns.u - coupledata)**2)
                cons = nutils.solver.optimize(
                    'lhs', sqr, droptol=1e-15, constrain=cons0)
            else:
                res = res0 + couplingsampleGP.integral(ns.basis * coupledata)

        if interface.is_action_required(action_write_iteration_checkpoint()):
            print("Writing iteration checkpoint")
            lhscheckpoint = lhs0
            interface.mark_action_fulfilled(
                action_write_iteration_checkpoint())
            bezier = domain.sample('bezier', 9)
            x, u, uexact = bezier.eval(['x_i', 'u', 'uexact'] @ ns, lhs=lhs0)
            with treelog.add(treelog.DataLog()):
                nutils.export.vtk('output/solution-' + side,
                                  bezier.tri, x, fem=u, exact=uexact)

        dt = min(timestep, precice_dt)

        lhs = nutils.solver.solve_linear(
            'lhs', res, constrain=cons, arguments=dict(lhs0=lhs0, dt=dt))

        if interface.is_write_data_required(dt):
            if side == 'Dirichlet':
                fluxvalues = res.eval(lhs0=lhs0, lhs=lhs, dt=dt)
                writedata = couplingsampleCC.eval(
                    'flux' @ ns, fluxdofs=fluxdofs(fluxvalues))
            else:
                writedata = couplingsampleCC.eval('u' @ ns, lhs=lhs)

            interface.write_block_scalar_data(writedataID,
                                              couplingsampleCC.npoints,
                                              dataIndicesCC, writedata)

        precice_dt = interface.advance(dt)

        if interface.is_action_required(action_read_iteration_checkpoint()):
            print("Reading iteration checkpoint")
            interface.mark_action_fulfilled(action_read_iteration_checkpoint())
            lhs0 = lhscheckpoint
        else:
            print("Advancing in time")
            lhs0 = lhs

    interface.finalize()

    # Once all entries of ``?lhs`` are establised, the corresponding solution can
    # be vizualised by sampling values of ``ns.u`` along with physical
    # coordinates ``ns.x``, with the solution vector provided via the
    # ``arguments`` dictionary. The sample members ``tri`` and ``hull`` provide
    # additional inter-point information required for drawing the mesh and
    # element outlines.

    # To confirm that our computation is correct, we use our knowledge of the
    # analytical solution to evaluate the L2-error of the discrete result.

    err = domain.integral('(u - uexact)^2 d:x' @ ns,
                          degree=degree * 2).eval(lhs=lhs)**.5
    nutils.log.user('L2 error: {:.2e}'.format(err))

    return cons, lhs, err

# If the script is executed (as opposed to imported), :func:`nutils.cli.run`
# calls the main function with arguments provided from the command line. For
# example, to keep with the default arguments simply run :sh:`python3
# laplace.py`. To select mixed elements and quadratic basis functions add
# :sh:`python3 laplace.py etype=mixed degree=2`.


if __name__ == '__main__':
    nutils.cli.run(main)
    # nutils.cli.run(laplace)