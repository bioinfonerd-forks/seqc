import numpy as np
from numpy import ma
import pandas as pd
import multiprocessing
from sklearn.neighbors import NearestNeighbors
import warnings
import time
from scipy.sparse import csr_matrix, find
from scipy.sparse.linalg import eigs
from numpy.linalg import norm
from statsmodels.sandbox.stats.multicomp import multipletests
from scipy.stats.mstats import kruskalwallis, rankdata
from scipy.stats import t
from tinydb import TinyDB, Query
from functools import partial
from collections import namedtuple


def keigs(T, k, P, take_diagonal=0):
    """ return k largest magnitude eigenvalues for the matrix T.
    :param T: Matrix to find eigen values/vectors of
    :param k: number of eigen values/vectors to return
    :param P: in the case of symmetric normalizations,
              this is the NxN diagonal matrix which relates the nonsymmetric
              version to the symmetric form via conjugation
    :param take_diagonal: if 1, returns the eigenvalues as a vector rather than as a
                          diagonal matrix.
    """
    D, V = eigs(T, k, tol=1e-4, maxiter=1000)
    D = np.real(D)
    V = np.real(V)
    inds = np.argsort(D)[::-1]
    D = D[inds]
    V = V[:, inds]
    if P is not None:
        V = P.dot(V)

    # Normalize
    for i in range(V.shape[1]):
        V[:, i] = V[:, i] / norm(V[:, i])
    V = np.round(V, 10)

    if take_diagonal == 0:
        D = np.diag(D)

    return V, D


class GraphDiffusion:
    def __init__(self, knn=10, normalization='smarkov', epsilon=1,
                 n_diffusion_components=10):
        """
        Run diffusion maps on the data. This implementation is based on the
        diffusion geometry library in Matlab:
        https://services.math.duke.edu/~mauro/code.html#DiffusionGeom and was implemented
        by Pooja Kathail

        :param data: Data matrix of samples X features
        :param knn: Number of neighbors for graph construction to determine distances between cells
        :param normalization: method for normalizing the matrix of weights
             'bimarkov'            force row and column sums to be 1
             'markov'              force row sums to be 1
             'smarkov'             symmetric conjugate to markov
             'beltrami'            Laplace-Beltrami normalization ala Coifman-Lafon
             'sbeltrami'           symmetric conjugate to beltrami
             'FokkerPlanck'        Fokker-Planck normalization
             'sFokkerPlanck'       symmetric conjugate to Fokker-Planck normalization
        :param epsilon: Gaussian standard deviation for converting distances to affinities
        :param n_diffusion_components: Number of diffusion components to generate
        """
        if normalization not in ['bimarkov', 'smarkov', 'markov', 'sbeltrami', 'beltrami',
                                 'FokkerPlanck', 'sFokkerPlanck']:
            raise ValueError(
                'Unsupported normalization. Please refer to the docstring for the '
                'supported methods')

        self.knn = knn
        self.normalization = normalization
        self.epsilon = epsilon
        self.n_diffusion_components = n_diffusion_components
        self.eigenvectors = None
        self.eigenvalues = None
        self.diffusion_operator = None
        self.weights = None

    @staticmethod  # todo fix; what is S?
    def bimarkov(W, max_iters=100, abs_error=0.00001, verbose=False, **kwargs):

        if W.size == 0:
            return

        # process input
        if W.shape[0] != W.shape[1]:
            raise ValueError('Bimarkov.py: kernel must be NxN\n')

        N = W.shape[0]

        # initialize
        p = np.ones(N)

        # iterative
        for i in range(max_iters):

            S = np.ravel(S.sum(axis=1)).toarray()
            err = np.max(np.absolute(1.0 - np.max(S)), np.absolute(1.0 - np.min(S)))

            if err < abs_error:
                break

            D = csr_matrix((np.divide(1, np.sqrt(S)), (range(N), range(N))), shape=[N, N])
            p = S.dot(p)
            W = D.dot(W).dot(D)

        # iron out numerical errors
        T = (W + W.T) / 2
        return T, p

    @staticmethod
    def smarkov(D, N, W):
        D = csr_matrix((np.sqrt(D), (range(N), range(N))), shape=[N, N])
        P = D
        T = D.dot(W).dot(D)
        T = (T + T.T) / 2
        return T, P

    @staticmethod
    def markov(D, N, W):
        T = csr_matrix((D, (range(N), range(N))), shape=[N, N]).dot(W)
        return T, None

    @staticmethod
    def sbeltrami(D, N, W):
        P = csr_matrix((D, (range(N), range(N))), shape=[N, N])
        K = P.dot(W).dot(P)

        D = np.ravel(K.sum(axis=1))
        D[D != 0] = 1 / D[D != 0]

        D = csr_matrix((D, (range(N), range(N))), shape=[N, N])
        P = D
        T = D.dot(K).dot(D)

        T = (T + T.T) / 2
        return T, P

    @staticmethod
    def beltrami(D, N, W):
        D = csr_matrix((D, (range(N), range(N))), shape=[N, N])
        K = D.dot(W).dot(D)

        D = np.ravel(K.sum(axis=1))
        D[D != 0] = 1 / D[D != 0]

        V = csr_matrix((D, (range(N), range(N))), shape=[N, N])
        T = V.dot(K)
        return T, None

    @staticmethod
    def FokkerPlanck(D, N, W):
        D = csr_matrix((np.sqrt(D), (range(N), range(N))), shape=[N, N])
        K = D.dot(W).dot(D)

        D = np.ravel(K.sum(axis=1))
        D[D != 0] = 1 / D[D != 0]

        D = csr_matrix((D, (range(N), range(N))), shape=[N, N])
        T = D.dot(K)
        return T, None

    def sFokkerPlanck(self, D, N, W):
        print('(sFokkerPlanck) ... ')

        D = csr_matrix((np.sqrt(D), (range(N), range(N))), shape=[N, N])
        K = D.dot(W).dot(D)

        D = np.ravel(K.sum(axis=1))
        D[D != 0] = 1 / D[D != 0]

        D = csr_matrix((np.sqrt(D), (range(N), range(N))), shape=[N, N])
        P = D
        T = D.dot(K).dot(D)

        T = (T + T.T) / 2
        return T, P

    def fit(self, data, verbose=True):
        """
        :return: Dictionary containing diffusion operator, weight matrix,
                 diffusion eigen vectors, and diffusion eigen values
        """
        if verbose:
            print('Running Diffusion maps with the following parameters:')
            print('Normalization: %s' % self.normalization)
            print('Number of nearest neighbors k: %d' % self.knn)
            print('Epsilon: %.4f' % self.epsilon)

        # Nearest neighbors
        start = time.process_time()
        N = data.shape[0]
        nbrs = NearestNeighbors(n_neighbors=self.knn).fit(data)
        distances, indices = nbrs.kneighbors(data)

        # Adjacency matrix
        rows = np.zeros(N * self.knn, dtype=np.int32)
        cols = np.zeros(N * self.knn, dtype=np.int32)
        dists = np.zeros(N * self.knn)
        location = 0
        for i in range(N):
            inds = range(location, location + self.knn)
            rows[inds] = indices[i, :]
            cols[inds] = i
            dists[inds] = distances[i, :]
            location += self.knn
        W = csr_matrix((dists, (rows, cols)), shape=[N, N])

        # Symmetrize W
        W = W + W.T

        # Convert to affinity (with selfloops)
        rows, cols, dists = find(W)
        rows = np.append(rows, range(N))
        cols = np.append(cols, range(N))
        dists = np.append(dists / (self.epsilon ** 2), np.zeros(N))
        W = csr_matrix((np.exp(-dists), (rows, cols)), shape=[N, N])

        # Create D
        D = np.ravel(W.sum(axis=1))
        D[D != 0] = 1 / D[D != 0]

        # Go through the various normalizations
        fnorm = getattr(self, self.normalization)
        T, P = fnorm(D=D, N=N, W=W)

        if self.normalization != 'bimarkov' and verbose:
            print('%.2f seconds' % (time.process_time() - start))

        # Eigen value decomposition
        V, D = keigs(T, self.n_diffusion_components, P, take_diagonal=1)
        self.eigenvectors = V
        self.eigenvalues = D
        self.diffusion_operator = T
        self.weights = W


class PCA:
    def __init__(self, n_components=100):
        self.n_components = n_components
        self.loadings = None
        self.eigenvalues = None

    def fit(self, data):

        if isinstance(data, pd.DataFrame):
            X = data.values
        elif isinstance(data, np.ndarray):
            X = data
        else:
            raise TypeError('data must be a pd.DataFrame or np.ndarray')

        # Make sure data is zero mean
        X = np.subtract(X, np.amin(X))
        X = np.divide(X, np.amax(X))

        # Compute covariance matrix
        if X.shape[1] < X.shape[0]:
            C = np.cov(X, rowvar=0)
        # if N > D, we better use this matrix for the eigendecomposition
        else:
            C = np.multiply((1 / X.shape[0]), np.dot(X, X.T))

        # Perform eigendecomposition of C
        C[np.where(np.isnan(C))] = 0
        C[np.where(np.isinf(C))] = 0
        l, M = np.linalg.eig(C)

        # Sort eigenvectors in descending order
        ind = np.argsort(l)[::-1]
        l = l[ind]
        if self.n_components < 1:
            self.n_components = (
                np.where(np.cumsum(np.divide(l, np.sum(l)), axis=0) >=
                         self.n_components)[0][0] + 1)
            print('Embedding into ' + str(self.n_components) + ' dimensions.')
        elif self.n_components > M.shape[1]:
            self.n_components = M.shape[1]
            print('Target dimensionality reduced to ' + str(self.n_components) + '.')

        M = M[:, ind[:self.n_components]]
        l = l[:self.n_components]

        # Apply mapping on the data
        if X.shape[1] >= X.shape[0]:
            M = np.multiply(np.dot(X.T, M), (1 / np.sqrt(X.shape[0] * l)).T)

        self.loadings = M
        self.eigenvalues = l

    def transform(self, data, n_components=None):
        if n_components is None:
            n_components = self.n_components
        projected = np.dot(data, self.loadings[:, :n_components])
        if isinstance(data, pd.DataFrame):
            return pd.DataFrame(projected, index=data.index)
        else:
            return projected

    def fit_transform(self, data):
        self.fit(data)
        return self.transform(data)


class correlation:

    @staticmethod
    def vector(x: np.array, y: np.array):
        # x = x[:, np.newaxis]  # for working with matrices
        mu_x = x.mean()  # cells
        mu_y = y.mean(axis=0)  # cells by gene --> cells by genes
        sigma_x = x.std()
        sigma_y = y.std(axis=0)

        return ((y * x).mean(axis=0) - mu_y * mu_x) / (sigma_y * sigma_x)

    @staticmethod
    def map(x, y):
        """Correlate each n with each m.

        :param x: np.array; shape N x T.
        :param y: np.array; shape M x T.
        :returns: np.array; shape N x M in which each element is a correlation
                            coefficient.

        """
        mu_x = x.mean(1)
        mu_y = y.mean(1)
        n = x.shape[1]
        if n != y.shape[1]:
            raise ValueError('x and y must ' +
                             'have the same number of timepoints.')
        s_x = x.std(1, ddof=n - 1)
        s_y = y.std(1, ddof=n - 1)
        cov = np.dot(x,
                     y.T) - n * np.dot(mu_x[:, np.newaxis],
                                       mu_y[np.newaxis, :])
        return cov / np.dot(s_x[:, np.newaxis], s_y[np.newaxis, :])

    @staticmethod
    def eigv(evec, data, components=tuple(), knn=10):

        if isinstance(data, pd.DataFrame):
            D = data.values
            df = True
        elif isinstance(data, np.ndarray):
            D = data
            df = False
        else:
            raise TypeError('data must be a pd.DataFrame or np.ndarray')

        # set components, remove zero if it was specified
        if not components:
            components = np.arange(evec.shape[1])
        else:
            components = np.array(components)
        components = components[components != 0]

        eigv_corr = np.empty((D.shape[1], evec.shape[1]), dtype=np.float)
        # assert eigv_corr.shape == (gene_shape, component_shape),
        #     '{!r}, {!r}'.format(eigv_corr.shape,
        #                         (gene_shape, component_shape))

        for component_index in components:
            component_data = evec[:, component_index]

            # assert component_data.shape == (cell_shape,), '{!r} != {!r}'.format(
            #     component_data.shape, (cell_shape,))
            order = np.argsort(component_data)
            # x = pd.rolling_mean(pd.DataFrame(component_data[order]), knn)[knn:].values
            x = pd.DataFrame(component_data[order]).rolling(
                window=knn, center=False).mean()[knn:].values
            # assert x.shape == (cell_shape - no_cells,)

            # this fancy indexing will copy self.molecules
            # vals = pd.rolling_mean(pd.DataFrame(D[order, :]), knn, axis=0)[
            #        knn:].values
            vals = pd.DataFrame(D[order, :]).rolling(
                window=knn, center=False, axis=0).mean()[knn:].values
            # assert vals.shape == (cell_shape - no_cells, gene_shape)

            eigv_corr[:, component_index] = correlation.vector(x, vals)

        # this is sorted by order, need it in original order (reverse the sort)

        eigv_corr = eigv_corr[:, components]
        if df:
            eigv_corr = pd.DataFrame(eigv_corr, index=data.columns, columns=components)
        return eigv_corr


class smoothing:

    @staticmethod
    def kneighbors(data, n_neighbors=50):
        """
        :param data: np.ndarray | pd.DataFrame; genes x cells array
        :param n_neighbors: int; number of neighbors to smooth over
        :return: np.ndarray | pd.DataFrame; same as input
        """

        if isinstance(data, pd.DataFrame):
            df = True
            data_ = data.values
        elif isinstance(data, np.ndarray):
            df = False
            data_ = data
        else:
            raise TypeError("data must be a pd.DataFrame or np.ndarray")

        knn = NearestNeighbors(
            n_neighbors=n_neighbors,
            n_jobs=multiprocessing.cpu_count() - 1)

        knn.fit(data_)
        dist, inds = knn.kneighbors(data_)

        # set values equal to their means across neighbors
        res = data_[inds, :].mean(axis=1)

        if df:
            res = pd.DataFrame(res, index=data.index,
                               columns=data.columns)
        return res


class GSEA:

    def __init__(self, correlations, labels=None):
        """
        :param correlations: pd.Series or iterable of pd.Series;
          objects containing correlations. If a single series is passed, the main process
          will compute gene set enrichments. If an iterable is passed, processes will be
          spawned to process each row in the correlation array. If the pd.Series contain
          name fields, these labels will be retained in the output.
        :param labels: iterable; alternatively, labels can be passed directly to GSEA.
          these will overwrite any names given in provided Series, and must be unique.

        Producing correlation vectors:
        (1) Correlation is a natural measure of similarity for diffusion components or
            PCA components. One can correlate the expression value of each gene with its
            loading for that vector.
        (2) Correlation can be used to measure association with a phenotype label (e.g.
            a cluster). For multi-cluster association, a one vs other binary labeling is
            appropriate in many cases. In circumstances where clusters have known
            progression, correlation can be run against ordered clusters.

        """
        if isinstance(correlations, pd.Series):
            self.correlations = [correlations,]
            self.nproc = 1
        elif all(isinstance(obj, pd.Series) for obj in correlations):
            self.correlations = list(correlations)
            self.nproc = min((multiprocessing.cpu_count() - 1, len(correlations)))
        else:
            raise ValueError('correlations must be passed as pd.Series objects.')

        # set correlation labels
        if labels:
            if not len(labels) == len(self.correlations):
                raise ValueError(
                    'number of passed labels ({!s}) does not match number of passed '
                    'correlations ({!s})'.format(
                        len(labels), len(self.correlations)))
            if not len(labels) == len(set(labels)):
                raise ValueError('labels cannot contain duplicates.')
            for c, l in zip(self.correlations, labels):
                c.name = l
        else:
            for c in self.correlations:
                try:
                    getattr(c, 'name')
                except AttributeError:  # at least one series lacks a name; reset all
                    for i, c in enumerate(self.correlations):
                        self.correlations[i] = c.copy()  # don't mutate original vector
                        self.correlations[i].name = i
                    break
            if not len(set(c.name for c in self.correlations)) == len(self.correlations):
                raise ValueError('correlation Series names must be unique')

    class Database:

        @staticmethod
        def create(gene_set_files, file_labels, db_name):
            """
            :param gene_set_files: [str, ...]; GSEA .gmt files to add to the database
            :param file_labels: [str, ...]; labels corresponding to each of the gene_set_files.
              Must be the same length as gene_set_files
            :param db_name: str; path and name of .json file in which to store database.
              ".json" will be appended to to the filename if db_name does not end with
              that suffix.
            :return: TinyDB database
            """
            records = []
            for file_, label in zip(gene_set_files, file_labels):
                with open(file_, 'r') as f:
                    sets = [l.strip().split('\t') for l in f.readlines()]
                records.extend(
                    {'type': label.upper(), 'name': fields[0], 'genes': fields[2:]} for
                    fields in sets)

            if not db_name.endswith('.json'):
                db_name += '.json'
            db = TinyDB(db_name)
            db.insert_multiple(records)
            return db

        @staticmethod
        def load(db_name):
            """wrapper for TinyDB loading
            :param db_name:  database to load
            :return: TinyDB database
            """
            return TinyDB(db_name)

        @staticmethod
        def select(data, field):
            """return field from each data entry extracted from a database

            # todo add example usage

            :param data: [dict, ...]; output of a database Query
            :param field: str; key for field to be extracted
            :return: list; field for each entry in data
            """
            return list(map(lambda x: x[field], data))

    class SetMasks:

        @staticmethod
        def from_db(sets, correlations):
            set_genes = GSEA.Database.select(sets, 'genes')

            # map genes to index; should the map consider the union?
            gene_map = dict(zip(
                correlations.index, np.arange(correlations.shape[0])))

            masks = np.zeros((len(sets), len(gene_map)), dtype=np.bool)
            for i, set_ in enumerate(set_genes):
                masks[i, [gene_map[g] for g in
                          set(set_).intersection(gene_map.keys())]] = 1
            return masks

    @staticmethod
    def _gsea_process(correlations, sets, n_perm, alpha):
        """
        :param correlations: pd.Series; an ordered list of correlations
        :param sets: gene sets extracted from GSEA database
        :param n_perm: int; number of permutations to use in constructing the null
          distribution for significance testing
        :alpha: float; percentage of type I error to use when computing False Discovery
          Rate correction.
        """
        set_names = GSEA.Database.select(sets, 'name')
        masks = GSEA.SetMasks.from_db(sets, correlations)
        pos, neg = GSEA.construct_normalized_correlation_matrices(
            correlations, masks)
        p, p_adj, es = GSEA.calculate_enrichment_significance(
            pos, neg, n_perm=n_perm, alpha=alpha)
        return pd.DataFrame(
                {'p': p, 'p_adj': p_adj, 'es': es},
                index=set_names)

    def test(self, sets, n_perm=1000, alpha=0.05):
        """
        :param sets: gene sets to test against ordered correlations # todo type?
        :param n_perm: int; number of permutations to use in constructing the null
          distribution for significance testing
        :alpha: float; percentage of type I error to use when computing False Discovery
          Rate correction.
        """
        partial_gsea_process = partial(
            self._gsea_process,
            sets=sets,
            n_perm=n_perm,
            alpha=alpha)

        # todo
        # can split sets in cases where the number of samples is smaller than the number
        # of processes; this will ensure parallelization is maximized in all circumstances

        pool = multiprocessing.Pool(self.nproc)
        res = pool.map(
            partial_gsea_process, self.correlations)
        pool.close()

        return {c.name: df for (c, df) in zip(self.correlations, res)}

    @staticmethod
    def construct_normalized_correlation_matrices(correlations, set_masks):

        N = np.tile(correlations, (set_masks.shape[0], 1))  # tile one row per set mask

        # create arrays masked based on whether or not genes were in the gene set
        pos = ma.masked_array(N, mask=set_masks)
        # copy for neg, else can't use in-place operations
        neg = ma.masked_array(N.copy(), mask=~set_masks.copy())
        neg.fill(1)  # negative decrements are binary, not based on correlations

        # normalize positive and negative arrays
        pos /= ma.sum(pos, axis=1)[:, np.newaxis]
        neg /= ma.sum(neg, axis=1)[:, np.newaxis]

        return pos, neg

    @staticmethod
    def calculate_enrichment_score(pos, neg):
        """
        :param pos: n x k matrix of n gene_sets x k normalized gene correlations where
          genes were in sets, else 0.
        :param neg: n x k matrix of n gene_sets x k genes where entries are equal to
          1 / k where genes were not in sets, else 0.
        :return: vector of maximum enrichment scores
        """
        cumpos = pos.filled(0).cumsum(axis=1)
        cumneg = neg.filled(0).cumsum(axis=1)
        es = cumpos - cumneg
        return es[np.arange(es.shape[0]), np.argmax(np.abs(es), axis=1)]

    @staticmethod
    def permute_correlation_ranks(pos_corr_matrix, neg_corr_matrix):
        # catch numpy.ma warnings about in-place mask modification, it is warning us
        # about a desired feature.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            np.random.shuffle(pos_corr_matrix)
            np.random.shuffle(neg_corr_matrix)

    @staticmethod
    def calculate_enrichment_significance(pos, neg, n_perm=1000, alpha=0.05):
        es = GSEA.calculate_enrichment_score(pos, neg)  # score

        # calculate permutations
        perms = np.zeros((n_perm, pos.shape[0]), dtype=np.float)  # n_perm x num sets
        i = 0
        while True:
            GSEA.permute_correlation_ranks(pos, neg)
            perms[i, :] = GSEA.calculate_enrichment_score(pos, neg)
            i += 1
            if i >= n_perm:
                break

        # calculate p-vals
        pvals = 1 - np.sum(perms < np.abs(es[np.newaxis, :]), axis=0) / perms.shape[0]

        # calculate adjusted p-vals
        adj_p = multipletests(pvals, alpha=alpha, method='fdr_tsbh')[1]

        return pvals, adj_p, es

        # def plot_enrichment_scores(pos, neg, ax=None, fig=None):
        #     cumpos = pos.filled(0).cumsum(axis=1)
        #     cumneg = neg.filled(0).cumsum(axis=1)
        #     edges = cumpos - cumneg
        #     es_loc = np.argmax(np.abs(edges), axis=1)
        #     es = edges[np.arange(edges.shape[0]), es_loc]
        #     if not fig:
        #         fig = plt.gcf()
        #     if not ax:
        #         ax = plt.gca()
        #     ax.plot((cumpos - cumneg).T, linewidth=1, color='royalblue')
        #     xlim = ax.get_xlim()
        #     ax.hlines(0, *xlim, linewidth=1)
        #     sns.despine(top=True, bottom=True, right=True)
        #     seqc.plot.detick(ax, x=True, y=False)
        #     plt.scatter(es_loc, es, marker='o', facecolors='none', edgecolors='indianred', s=20, linewidth=1)


class DifferentialExpression:

    def __init__(self, data, group_assignments, alpha=0.05):
        """
        :param data: n cells x k genes 2d array
        :param group_assignments: n cells 1d vector
        :param alpha: float (0, 1], acceptable type I error
        """
        # make sure group_assignments and data have the same length
        if not data.shape[0] == group_assignments.shape[0]:
            raise ValueError(
                'Group assignments shape ({!s}) must equal the number of rows in data '
                '({!s}).'.format(group_assignments.shape[0], data.shape[0]))

        # todo
        # may want to verify that each group has at least two observations
        # (else variance won't work)

        # store index if both data and group_assignments are pandas objects
        if isinstance(data, pd.DataFrame) and isinstance(group_assignments, pd.Series):
            # ensure assignments and data indices are aligned
            try:
                ordered_assignments = group_assignments[data.index]
                if not len(ordered_assignments) == data.shape[0]:
                    raise ValueError(
                        'Index mismatch between data and group_assignments detected when '
                        'aligning indices. check for duplicates.')
            except:
                raise ValueError('Index mismatch between data and group_assignments.')

            # sort data by cluster assignment
            idx = np.argsort(ordered_assignments.values)
            self.data = data.iloc[idx, :].values
            ordered_assignments = ordered_assignments.iloc[idx]
            self.group_assignments = ordered_assignments.values
            self.index = data.columns

        else:  # get arrays from input values
            self.index = None  # inputs were not all indexed pandas objects

            try:
                data = np.array(data)
            except:
                raise ValueError('data must be convertible to a np.ndarray')

            try:
                group_assignments = np.array(group_assignments)
            except:
                raise ValueError('group_assignments must be convertible to a np.ndarray')

            idx = np.argsort(group_assignments)
            self.data = data[idx, :]
            self.group_assignments = group_assignments[idx]

        self.post_hoc = None
        self.groups = np.unique(group_assignments)

        # get points to split the array, create slicers for each group
        self.split_indices = np.where(np.diff(self.group_assignments))[0] + 1
        # todo is this a faster way of calculating the below anova?
        # self.array_views = np.array_split(self.data, self.split_indices, axis=0)

        if not 0 < alpha <= 1:
            raise ValueError('Parameter alpha must fall within the interval (0, 1].')
        self.alpha = alpha

        self._anova = None

    # todo implement min_mean_expr
    def anova(self, min_mean_expr=None):
        """
        :param min_mean_expr: minimum average gene expression value that must be reached
          in at least one cluster for the gene to be considered
        :return:
        """
        if self._anova is not None:
            return self._anova

        # run anova
        f = lambda v: kruskalwallis(*np.split(v, self.split_indices))[1]
        pvals = np.apply_along_axis(f, 0, self.data)  # todo could shunt to a multiprocessing pool

        # correct the pvals
        _, pval_corrected, _, _ = multipletests(pvals, self.alpha, method='fdr_tsbh')

        # store data & return
        if self.index is not None:
            self._anova = pd.Series(pval_corrected, index=self.index)
        else:
            self._anova = pval_corrected
        return self._anova

    def post_hoc_tests(self):
        """
        carries out post-hoc tests using Welch's U-test on ranked data.
        """
        if self._anova is None:
            self.anova()

        anova_significant = np.array(self._anova) < 1  # call array in case it is a Series

        # limit to significant data, convert to column-wise ranks.
        data = self.data[:, anova_significant]
        rank_data = np.apply_along_axis(rankdata, 0, data)
        # assignments = self.group_assignments[anova_significant]

        split_indices = np.where(np.diff(self.group_assignments))[0] + 1
        array_views = np.array_split(rank_data, split_indices, axis=0)

        # get mean and standard deviations of each
        fmean = partial(np.mean, axis=0)
        fvar = partial(np.var, axis=0)
        mu = np.vstack(list(map(fmean, array_views))).T  # transpose to get gene rows
        n = np.array(list(map(lambda x: x.shape[0], array_views)))
        s = np.vstack(list(map(fvar, array_views))).T
        s_norm = s / n  # transpose to get gene rows

        # calculate T
        numerator = mu[:, np.newaxis, :] - mu[:, :, np.newaxis]
        denominator = np.sqrt(s_norm[:, np.newaxis, :] + s_norm[:, :, np.newaxis])
        statistic = numerator / denominator

        # calculate df
        s_norm2 = s**2 / (n**2 * n-1)
        numerator = (s_norm[:, np.newaxis, :] + s_norm[:, :, np.newaxis]) ** 2
        denominator = (s_norm2[:, np.newaxis, :] + s_norm2[:, :, np.newaxis])
        df = np.floor(numerator / denominator)

        # get significance
        p = t.cdf(np.abs(statistic), df)  # note, two tailed test

        # calculate fdr correction; because above uses 2-tails, alpha here is halved
        # because each test is evaluated twice due to the symmetry of vectorization.
        p_adj = multipletests(np.ravel(p), alpha=self.alpha, method='fdr_tsbh')[1]
        p_adj = p_adj.reshape(*p.shape)

        phr = namedtuple('PostHocResults', ['p_adj', 'statistic', 'mu'])
        self.post_hoc = phr(p_adj, statistic, mu)

        if self.index is not None:
            p_adj = pd.Panel(
                p_adj, items=self.index[anova_significant], major_axis=self.groups,
                minor_axis=self.groups)
            statistic = pd.Panel(
                statistic, items=self.index[anova_significant], major_axis=self.groups,
                minor_axis=self.groups)
            mu = pd.DataFrame(mu, self.index[anova_significant], columns=self.groups)

        return p_adj, statistic, mu

    def population_markers(self, p_crit=0.0):
        """
        Return markers that are significantly differentially expressed in one
        population vs all others

        :param p_crit: float, fraction populations that may be indistinguishable from the
          highest expressing population for each gene. If zero, each marker gene is
          significantly higher expressed in one population relative to all others.
          If 0.1, 10% of populations may share high expression of a gene, and those
          populations will be marked as expressing that gene.

        """
        if self.post_hoc is None:
            self.post_hoc_tests()

        # get highest mean for each gene
        top_gene_idx = np.argmax(self.post_hoc.mu, axis=1)

        # index p_adj first dimension with each sample, will reduce to 2d genes x samples
        top_gene_sig = self.post_hoc.p_adj[:, top_gene_idx, :]

        # for each gene, count the number of non-significant DE results.
        sig = np.array(top_gene_sig < self.alpha)
        num_sig = np.sum(sig, axis=2)

        # if this is greater than N - 1 * p_crit, discard the gene.
        n = self.post_hoc.p_adj.shape[2] - 1  # number of genes, sub 1 for self
        idx_marker_genes = np.where(num_sig < n * (1 - p_crit))
        marker_genes = sig[idx_marker_genes, :]

        # correctly index these genes
        if self.index:
            pass  # todo fix this

        return marker_genes



























