package com.javaproject.demo.mapper;


import com.javaproject.demo.dto.QuestionQueryDTO;
import com.javaproject.demo.model.Question;

import java.util.List;


    public interface QuestionExtMapper {
        int incView(Question record);

        int incCommentCount(Question record);

        List<Question> selectRelated(Question question);

        Integer countBySearch(QuestionQueryDTO questionQueryDTO);

        List<Question> selectBySearch(QuestionQueryDTO questionQueryDTO);

        // Integer countBySearch(QuestionQueryDTO questionQueryDTO);

        //List<Question> selectBySearch(QuestionQueryDTO questionQueryDTO);
    }
