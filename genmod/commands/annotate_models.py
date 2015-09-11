#!/usr/bin/env python
# encoding: utf-8
"""
annotate_models.py

Command line tool for annotating what genetic models that are followed for variants in the vcf format.
The variants needs to be annotated with what genetic regions they belong to and preferably if they are exonic or not.
This can be done with 'genmod annotate_regions', vep or other tools. Please specify on the command line.

Created by Måns Magnusson on 2015-08-25.
Copyright (c) 2015 __MoonsoInc__. All rights reserved.
"""

from __future__ import (print_function)

import sys
import os
import click
import inspect
import logging
import shutil

from multiprocessing import JoinableQueue, Manager, cpu_count
from codecs import open
from datetime import datetime
from tempfile import mkdtemp, TemporaryFile, NamedTemporaryFile


from ped_parser import FamilyParser
from vcf_parser import VCFParser

from genmod import (__version__)

from genmod.utils import (get_batches, VariantPrinter, check_individuals)
from genmod.annotate_models import (VariantAnnotator)
from genmod.vcf_tools import (add_metadata, print_headers, sort_variants, 
print_variant)

@click.command()
@click.argument('variant_file', 
                    nargs=1, 
                    type=click.Path(exists=True),
                    metavar='<vcf_file> or -'
)
@click.option('-f', '--family_file',
                    nargs=1, 
                    type=click.File('r'),
                    metavar='<ped_file>'
)
@click.option('-t' ,'--family_type', 
                type=click.Choice(['ped', 'alt', 'cmms', 'mip']), 
                default='ped',
                help='If the analysis use one of the known setups, please specify which one.'
)
@click.option('--vep', 
                    is_flag=True,
                    help='If variants are annotated with the Variant Effect Predictor.'
)
@click.option('--phased', 
                    is_flag=True,
                    help='If data is phased use this flag.'
)
@click.option('-s' ,'--strict', 
                    is_flag=True,
                    help='If strict model annotations should be used(see documentation).'
)
@click.option('-split' ,'--split_variants', 
                    is_flag=True,
                    help='If the variants should be splitted.'
)
@click.option('-p', '--processes', 
                default=min(4, cpu_count()),
                help='Define how many processes that should be use for annotation.'
)
@click.option('--silent', 
                    is_flag=True,
                    help='Do not print the variants.'
)
@click.option('-g' ,'--whole_gene', 
                    is_flag=True,
                    help="""If compounds should be checked in the whole gene regions. 
                    Not only exonic/splice sites."""
)
@click.option('-o', '--outfile',
                    type=click.File('w'),
                    help='Specify the path to a file where results should be stored.'
)
def annotate_models(variant_file, family_file, family_type, vep,
                    split_variants, phased, strict, silent, processes, 
                    whole_gene, outfile):
    """
    Annotate variants with what genetic models that are followed in a VCF file.
    The analysis is family based so each family that are specified in the family
    file and exists in the variant file will get it's own annotation.
    """
    logger = logging.getLogger(__name__)
    # For testing only:
    logger = logging.getLogger("genmod.commands.annotate")
    
    ######### This is for logging the command line string #########
    frame = inspect.currentframe()
    args, _, _, values = inspect.getargvalues(frame)
    argument_list = [
        i+'='+str(values[i]) for i in values if values[i] and 
        i not in ['frame']
    ]
    
    ###########################################################################
    
    logger.info("Running GENMOD annotate version {0}".format(__version__))
    logger.debug("Arguments: {0}".format(', '.join(argument_list)))
    
    if not family_file:
        print("Please provide a family file with -f/--family_file")
        sys.exit(1)
    
    logger.info("Setting up a family parser")
    family_parser = FamilyParser(family_file, family_type)
    logger.debug("Family parser done")
    # The individuals in the ped file must be present in the variant file:
    families = family_parser.families
    logger.info("Families used in analysis: {0}".format(
                    ','.join(list(families.keys()))))
    logger.info("Individuals included in analysis: {0}".format(
                    ','.join(list(family_parser.individuals.keys()))))
    
    
    logger.debug("Setting up a variant parser")
    if variant_file == '-':
        variant_parser = VCFParser(
            fsock = sys.stdin,
            split_variants=split_variants,
            check_info=False
            )
    else:
        variant_parser = VCFParser(
            infile = variant_file,
            split_variants=split_variants,
            check_info=False
            )
    logger.debug("Variant parser setup")
    
                
    head = variant_parser.metadata
    
    if "GeneticModels" in head.info_dict:
        logger.warning("Genetic models are already annotated according to vcf"\
        " header.")
        logger.info("Exiting...")
        sys.exit(1)
    
    vcf_individuals = variant_parser.individuals
    logger.debug("Individuals found in vcf file: {}".format(', '.join(vcf_individuals)))
    
    
    if vep:
        logger.info("Using VEP annotation")
        
    start_time_analysis = datetime.now()
    
    logger.info("Adding genmod version to vcf header")
    head.add_version_tracking(
                    'genmod',
                    __version__,
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    command_line_string=' '.join(argument_list)
                )
    
    logger.debug("Version added")
    logger.info("Adding genetic models to vcf header")
    add_metadata(
        head,
        'info',
        'GeneticModels',
        annotation_number='.',
        entry_type='String',
        description="':'-separated list of genetic models for this variant."
    )
    logger.debug("Genetic models added")
    logger.info("Adding model score to vcf header")
    add_metadata(
        head,
        'info',
        'ModelScore',
        annotation_number='1',
        entry_type='Integer',
        description="PHRED score for genotype models."
    )
    logger.debug("Model score added")
    logger.info("Adding Compounds to vcf header")
    add_metadata(
        head,
        'info',
        'Compounds',
        annotation_number='.',
        entry_type='String',
        description=("List of compound pairs for this variant."
        "The list is splitted on ',' family id is separated with compounds"
        "with ':'. Compounds are separated with '|'.")
    )
    logger.debug("Compounds added")
    
    try:
        check_individuals(family_parser.individuals, vcf_individuals)
    except IOError as e:
        logger.error(e)
        logger.info("Individuals in PED file: {0}".format(
                        ', '.join(family_parser.individuals)))
        logger.info("Individuals in VCF file: {0}".format(', '.join(vcf_individuals)))
        logger.info("Exiting...")
        sys.exit(1)

    analysis_individuals = list(family_parser.individuals.keys())
    
    logger.info("Individuals used in analysis: {0}".format(
        ', '.join(analysis_individuals)))
    
    
    ###################################################################
    ### The task queue is where all jobs(in this case batches that  ###
    ### represents variants in a region) is put. The consumers will ###
    ### then pick their jobs from this queue.                       ###
    ###################################################################

    logger.debug("Setting up a JoinableQueue for storing variant batches")
    variant_queue = JoinableQueue(maxsize=1000)
    logger.debug("Setting up a Queue for storing results from workers")
    results = Manager().Queue()

    num_model_checkers = processes
    #Adapt the number of processes to the machine that run the analysis
    logger.info('Number of CPU:s {}'.format(cpu_count()))
    logger.info('Number of model checkers: {}'.format(num_model_checkers))

    # We use a temp file to store the processed variants
    logger.debug("Build a tempfile for printing the variants")
    temp_file = NamedTemporaryFile(delete=False)
    temp_file.close()


    # These are the workers that do the heavy part of the analysis
    logger.info('Seting up the workers')
    model_checkers = [
        VariantAnnotator(
            task_queue=variant_queue,
            results_queue=results,
            families=families,
            individuals=analysis_individuals,
            phased=phased,
            strict=strict,
            whole_gene=whole_gene,
            vep=vep
        )
        for i in range(num_model_checkers)
    ]
    logger.info('Starting the workers')
    for worker in model_checkers:
        logger.debug('Starting worker {0}'.format(worker))
        worker.start()

    # This process prints the variants to temporary files
    logger.info('Seting up the variant printer')
    variant_printer = VariantPrinter(
            task_queue=results,
            head=head,
            mode='chromosome', 
            outfile = temp_file.name
    )
    logger.info('Starting the variant printer process')
    variant_printer.start()

    start_time_variant_parsing = datetime.now()
    
    # This process parses the original vcf and create batches to put in the variant queue:
    logger.info('Start parsing the variants')
    chromosome_list = get_batches(
                                variant_parser,
                                variant_queue
                            )
    
    logger.debug("Put stop signs in the variant queue")
    for i in range(num_model_checkers):
        variant_queue.put(None)
    
    variant_queue.join()
    results.put(None)
    variant_printer.join()
    
    
    sort_variants(infile=temp_file.name, mode='chromosome')

    print_headers(head=head, outfile=outfile, silent=silent)

    with open(temp_file.name, 'r', encoding='utf-8') as f:
        for line in f:
            print_variant(
                variant_line=line,
                outfile=outfile,
                mode='modified',
                silent=silent
            )
    
    logger.debug("Removing temp file")
    os.remove(temp_file.name)
    logger.debug("Temp file removed")

    logger.info('Time for whole analyis: {0}'.format(
        str(datetime.now() - start_time_analysis)))
    

if __name__ == '__main__':
    from genmod import logger
    from genmod.log import init_log
    init_log(logger, loglevel="INFO")
    
    annotate_models()